from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any

from sqlalchemy import desc, func, select

from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    AIDecision,
    BusinessCase,
    CaseHistory,
    CaseLookupKey,
    DiscordMapping,
    DiscordOutbox,
    EvidenceSnapshot,
    GmailMessage,
    GmailPendingMessage,
    MailAttachment,
    MailEvent,
    NodeRun,
    WorkflowRun,
    RequestCase,
    RequestComponent,
    RequestItem,
)


def dashboard_data() -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=24)
    with session_scope() as session:
        run_count = session.scalar(
            select(func.count()).select_from(WorkflowRun).where(WorkflowRun.started_at_utc >= since)
        ) or 0
        success_count = session.scalar(
            select(func.count()).select_from(WorkflowRun).where(
                WorkflowRun.started_at_utc >= since,
                WorkflowRun.status == "SUCCEEDED",
            )
        ) or 0
        failed_count = session.scalar(
            select(func.count()).select_from(WorkflowRun).where(
                WorkflowRun.started_at_utc >= since,
                WorkflowRun.status.in_(["FAILED", "PARTIAL"]),
            )
        ) or 0
        pending_count = session.scalar(
            select(func.count()).select_from(GmailPendingMessage).where(
                GmailPendingMessage.status.in_(["PENDING", "RETRY_WAIT", "PROCESSING"])
            )
        ) or 0
        mail_count = session.scalar(select(func.count()).select_from(MailEvent)) or 0
        recent_runs = session.scalars(
            select(WorkflowRun).order_by(desc(WorkflowRun.started_at_utc)).limit(8)
        ).all()
        recent_errors = session.scalars(
            select(NodeRun)
            .where(NodeRun.status == "FAILED")
            .order_by(desc(NodeRun.finished_at_utc))
            .limit(10)
        ).all()
        recent_mails = session.execute(
            select(MailEvent, GmailMessage)
            .join(GmailMessage, MailEvent.gmail_message_id == GmailMessage.message_id)
            .order_by(desc(MailEvent.created_at_utc))
            .limit(8)
        ).all()
        return {
            "run_count": run_count,
            "success_count": success_count,
            "failed_count": failed_count,
            "pending_count": pending_count,
            "mail_count": mail_count,
            "recent_runs": list(recent_runs),
            "recent_errors": list(recent_errors),
            "recent_mails": list(recent_mails),
        }


def list_runs(workflow_type: str = "", status: str = "", limit: int = 200) -> list[WorkflowRun]:
    with session_scope() as session:
        statement = select(WorkflowRun)
        if workflow_type:
            statement = statement.where(WorkflowRun.workflow_type == workflow_type)
        if status:
            statement = statement.where(WorkflowRun.status == status)
        rows = session.scalars(statement.order_by(desc(WorkflowRun.started_at_utc)).limit(limit)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_run(run_id: str) -> tuple[WorkflowRun | None, list[NodeRun]]:
    with session_scope() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return None, []
        nodes = session.scalars(
            select(NodeRun)
            .where(NodeRun.workflow_run_id == run_id)
            .order_by(NodeRun.started_at_utc.asc(), NodeRun.node_key.asc())
        ).all()
        session.expunge(run)
        for row in nodes:
            session.expunge(row)
        return run, list(nodes)


def list_mails(status: str = "", query: str = "", limit: int = 200) -> list[tuple[MailEvent, GmailMessage]]:
    with session_scope() as session:
        statement = (
            select(MailEvent, GmailMessage)
            .join(GmailMessage, MailEvent.gmail_message_id == GmailMessage.message_id)
            .order_by(desc(MailEvent.created_at_utc))
        )
        if status:
            statement = statement.where(MailEvent.status == status)
        if query:
            token = f"%{query}%"
            statement = statement.where(
                GmailMessage.subject.like(token)
                | GmailMessage.sender.like(token)
                | GmailMessage.message_id.like(token)
                | GmailMessage.thread_id.like(token)
            )
        rows = session.execute(statement.limit(limit)).all()
        for event, message in rows:
            session.expunge(event)
            session.expunge(message)
        return list(rows)


def get_mail_detail(message_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        message = session.get(GmailMessage, message_id)
        event = session.scalar(select(MailEvent).where(MailEvent.gmail_message_id == message_id))
        if message is None or event is None:
            return None
        thread_messages = session.scalars(
            select(GmailMessage)
            .where(GmailMessage.thread_id == message.thread_id)
            .order_by(GmailMessage.internal_date_utc.asc())
        ).all()
        message_ids = [item.message_id for item in thread_messages]
        attachments = session.scalars(
            select(MailAttachment)
            .where(MailAttachment.gmail_message_id.in_(message_ids))
            .order_by(MailAttachment.gmail_message_id, MailAttachment.file_name)
        ).all()
        evidence = session.scalars(
            select(EvidenceSnapshot)
            .where(EvidenceSnapshot.mail_event_id == event.id)
            .order_by(EvidenceSnapshot.created_at_utc)
        ).all()
        nodes = session.scalars(
            select(NodeRun)
            .where(NodeRun.mail_event_id == event.id)
            .order_by(NodeRun.started_at_utc.asc(), NodeRun.node_key.asc())
        ).all()
        # 대표 메일은 thread_messages에도 포함될 수 있으므로 개별 expunge 시
        # 동일 ORM 객체를 두 번 분리하게 된다. 조회 결과 전체를 한 번에 분리한다.
        session.expunge_all()
        return {
            "message": message,
            "event": event,
            "thread_messages": list(thread_messages),
            "attachments": list(attachments),
            "evidence": list(evidence),
            "nodes": list(nodes),
        }


def parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def list_ai_decisions(limit: int = 200) -> list[tuple[AIDecision, GmailMessage]]:
    with session_scope() as session:
        rows = session.execute(
            select(AIDecision, GmailMessage)
            .join(MailEvent, AIDecision.mail_event_id == MailEvent.id)
            .join(GmailMessage, MailEvent.gmail_message_id == GmailMessage.message_id)
            .order_by(desc(AIDecision.created_at_utc))
            .limit(limit)
        ).all()
        session.expunge_all()
        return list(rows)


def get_ai_decision(decision_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        decision = session.get(AIDecision, decision_id)
        if decision is None:
            return None
        event = session.get(MailEvent, decision.mail_event_id)
        message = session.get(GmailMessage, event.gmail_message_id) if event else None
        case = (
            session.get(BusinessCase, event.business_case_id)
            if event and event.business_case_id
            else None
        )
        session.expunge_all()
        return {"decision": decision, "event": event, "message": message, "case": case}


def list_business_cases(limit: int = 200) -> list[BusinessCase]:
    with session_scope() as session:
        rows = session.scalars(
            select(BusinessCase).order_by(desc(BusinessCase.updated_at_utc)).limit(limit)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def get_business_case(case_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        case = session.get(BusinessCase, case_id)
        if case is None:
            return None
        history = session.scalars(
            select(CaseHistory)
            .where(CaseHistory.business_case_id == case.id)
            .order_by(desc(CaseHistory.revision))
        ).all()
        events = session.execute(
            select(MailEvent, GmailMessage)
            .join(GmailMessage, MailEvent.gmail_message_id == GmailMessage.message_id)
            .where(MailEvent.business_case_id == case.id)
            .order_by(MailEvent.created_at_utc)
        ).all()
        keys = session.scalars(
            select(CaseLookupKey)
            .where(CaseLookupKey.business_case_id == case.id)
            .order_by(CaseLookupKey.key_type, CaseLookupKey.key_value)
        ).all()
        request = session.scalar(
            select(RequestCase).where(RequestCase.business_case_id == case.id)
        )
        items = (
            session.scalars(
                select(RequestItem)
                .where(RequestItem.request_case_id == request.id)
                .order_by(RequestItem.sequence)
            ).all()
            if request
            else []
        )
        components = (
            session.scalars(
                select(RequestComponent)
                .where(RequestComponent.request_case_id == request.id)
                .order_by(RequestComponent.sequence)
            ).all()
            if request
            else []
        )
        mapping = session.scalar(
            select(DiscordMapping).where(DiscordMapping.business_case_id == case.id)
        )
        outbox = session.scalars(
            select(DiscordOutbox)
            .where(DiscordOutbox.business_case_id == case.id)
            .order_by(desc(DiscordOutbox.created_at_utc))
        ).all()
        session.expunge_all()
        return {
            "case": case,
            "history": list(history),
            "events": list(events),
            "keys": list(keys),
            "request_case": request,
            "items": list(items),
            "components": list(components),
            "mapping": mapping,
            "outbox": list(outbox),
        }


def list_outbox(limit: int = 300) -> list[tuple[DiscordOutbox, BusinessCase]]:
    with session_scope() as session:
        rows = session.execute(
            select(DiscordOutbox, BusinessCase)
            .join(BusinessCase, DiscordOutbox.business_case_id == BusinessCase.id)
            .order_by(desc(DiscordOutbox.created_at_utc))
            .limit(limit)
        ).all()
        session.expunge_all()
        return list(rows)
