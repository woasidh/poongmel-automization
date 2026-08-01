from __future__ import annotations

from sqlalchemy import func, select

from pungmail.repositories.database import session_scope
from pungmail.repositories.mail_store import (
    enqueue_messages,
    list_ready_messages,
    mark_failed,
)
from pungmail.repositories.models import GmailPendingMessage, MailEvent
from pungmail.repositories.tracking import TrackedNode, create_workflow_run, finish_workflow_run


def test_enqueue_is_idempotent_and_failure_is_retryable(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    messages = [{"id": "message-1", "threadId": "thread-1"}]

    assert enqueue_messages(messages, run_id) == 1
    assert enqueue_messages(messages, run_id) == 0
    assert [row["message_id"] for row in list_ready_messages(10)] == ["message-1"]

    with session_scope() as session:
        event = session.scalar(select(MailEvent).where(MailEvent.gmail_message_id == "message-1"))
        assert event is not None
        event_id = event.id

    mark_failed("message-1", event_id, RuntimeError("temporary"))

    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(MailEvent)) == 1
        pending = session.get(GmailPendingMessage, "message-1")
        assert pending is not None
        assert pending.status == "RETRY_WAIT"
        assert pending.attempt_count == 1
        assert pending.next_attempt_at_utc is not None


def test_tracking_records_success_and_redacts_secrets(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    with TrackedNode(run_id, "validate_scope", input_summary={"token": "secret"}) as node:
        node.set_output({"accepted": True})
    finish_workflow_run(run_id, "SUCCEEDED", {"count": 1})

    with session_scope() as session:
        from pungmail.repositories.models import NodeRun, WorkflowRun

        run = session.get(WorkflowRun, run_id)
        row = session.scalar(select(NodeRun).where(NodeRun.workflow_run_id == run_id))
        assert run is not None and run.status == "SUCCEEDED"
        assert row is not None and row.status == "SUCCEEDED"
        assert "secret" not in row.input_summary_json
        assert "***" in row.input_summary_json
