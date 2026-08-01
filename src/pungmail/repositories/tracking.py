from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
import json
from typing import Any

from sqlalchemy import func, select

from pungmail.observability.logging import redact
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import NodeRun, WorkflowRun


def json_text(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, default=str, sort_keys=True)


def create_workflow_run(
    workflow_type: str,
    *,
    trigger_type: str = "SCHEDULED",
    prefect_flow_run_id: str | None = None,
) -> str:
    with session_scope() as session:
        row = WorkflowRun(
            workflow_type=workflow_type,
            trigger_type=trigger_type,
            prefect_flow_run_id=prefect_flow_run_id,
            status="RUNNING",
            started_at_utc=datetime.now(UTC),
        )
        session.add(row)
        session.flush()
        return row.id


def finish_workflow_run(run_id: str, status: str, summary: Any) -> None:
    with session_scope() as session:
        row = session.get(WorkflowRun, run_id)
        if row is None:
            return
        row.status = status
        row.summary_json = json_text(summary)
        row.finished_at_utc = datetime.now(UTC)


class TrackedNode(AbstractContextManager["TrackedNode"]):
    def __init__(
        self,
        workflow_run_id: str,
        node_key: str,
        *,
        mail_event_id: str | None = None,
        branch_key: str | None = None,
        input_summary: Any = None,
        prefect_task_run_id: str | None = None,
    ) -> None:
        self.workflow_run_id = workflow_run_id
        self.node_key = node_key
        self.mail_event_id = mail_event_id
        self.branch_key = branch_key
        self.input_summary = input_summary or {}
        self.prefect_task_run_id = prefect_task_run_id
        self.node_run_id = ""
        self.output_summary: Any = {}

    def __enter__(self) -> "TrackedNode":
        with session_scope() as session:
            attempt = session.scalar(
                select(func.max(NodeRun.attempt)).where(
                    NodeRun.workflow_run_id == self.workflow_run_id,
                    NodeRun.mail_event_id == self.mail_event_id,
                    NodeRun.node_key == self.node_key,
                )
            )
            row = NodeRun(
                workflow_run_id=self.workflow_run_id,
                mail_event_id=self.mail_event_id,
                node_key=self.node_key,
                branch_key=self.branch_key,
                attempt=int(attempt or 0) + 1,
                prefect_task_run_id=self.prefect_task_run_id,
                status="RUNNING",
                input_summary_json=json_text(self.input_summary),
                started_at_utc=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            self.node_run_id = row.id
        return self

    def set_output(self, value: Any) -> None:
        self.output_summary = value

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        with session_scope() as session:
            row = session.get(NodeRun, self.node_run_id)
            if row is None:
                return False
            row.finished_at_utc = datetime.now(UTC)
            if exc is None:
                row.status = "SUCCEEDED"
                row.output_summary_json = json_text(self.output_summary)
            else:
                row.status = "FAILED"
                row.error_type = exc_type.__name__ if exc_type else "Exception"
                row.error_message = str(redact(str(exc)))[:4000]
        return False


def record_skipped_node(
    workflow_run_id: str,
    node_key: str,
    *,
    mail_event_id: str | None = None,
    branch_key: str | None = None,
    reason: str,
) -> None:
    with session_scope() as session:
        row = NodeRun(
            workflow_run_id=workflow_run_id,
            mail_event_id=mail_event_id,
            node_key=node_key,
            branch_key=branch_key,
            attempt=1,
            status="SKIPPED",
            input_summary_json="{}",
            output_summary_json=json_text({"reason": reason}),
            started_at_utc=datetime.now(UTC),
            finished_at_utc=datetime.now(UTC),
        )
        session.add(row)
