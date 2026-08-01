from __future__ import annotations

from sqlalchemy import select

from pungmail.repositories.database import session_scope
from pungmail.repositories.mail_store import enqueue_messages
from pungmail.repositories.models import AIDecision, MailEvent, NodeRun
from pungmail.repositories.tracking import create_workflow_run
from pungmail.services.decision_pipeline import decide_mail
from pungmail.workflows.mail_processing import route_category_task


class FailingAI:
    def decide(self, evidence, candidates):  # noqa: ANN001, ANN201
        raise TimeoutError("simulated timeout")


def _event_id(message_id: str) -> str:
    with session_scope() as session:
        event = session.scalar(select(MailEvent).where(MailEvent.gmail_message_id == message_id))
        assert event is not None
        return event.id


def test_ai_failure_is_persisted_and_becomes_hold(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "failed-ai", "threadId": "thread-failed"}], run_id)
    event_id = _event_id("failed-ai")
    evidence = {
        "target_message_id": "failed-ai",
        "thread_id": "thread-failed",
        "messages": [{"subject": "판정 실패 테스트"}],
        "attachments": [],
    }
    decision, decision_id, error = decide_mail(
        event_id,
        evidence,
        [],
        settings=isolated_settings,
        client=FailingAI(),  # type: ignore[arg-type]
    )
    assert decision.category.value == "보류"
    assert "TimeoutError" in (error or "")
    with session_scope() as session:
        stored = session.get(AIDecision, decision_id)
        assert stored is not None
        assert stored.status == "FAILED_TO_HOLD"
        assert stored.error_type == "TimeoutError"


def test_router_runs_only_selected_category_branch(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "route-mail", "threadId": "route-thread"}], run_id)
    event_id = _event_id("route-mail")
    route_category_task.fn(
        run_id,
        event_id,
        {
            "business_case_id": "case-route",
            "category": "사내업무",
            "status": "IN_PROGRESS",
        },
    )
    with session_scope() as session:
        rows = session.scalars(
            select(NodeRun).where(NodeRun.mail_event_id == event_id)
        ).all()
        by_key = {row.node_key: row.status for row in rows}
    assert by_key["route_category"] == "SUCCEEDED"
    assert by_key["process_internal_work"] == "SUCCEEDED"
    assert by_key["process_order_sources"] == "SKIPPED"
    assert by_key["process_hold"] == "SKIPPED"
