from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from typing import Any, Iterable

from sqlalchemy import or_, select

from pungmail.adapters.gmail.evidence import StoredAttachment
from pungmail.adapters.gmail.parser import ParsedMessage
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    EvidenceSnapshot,
    GmailMessage,
    GmailPendingMessage,
    MailAttachment,
    MailEvent,
    MonitorState,
)


def get_monitor_state(key: str) -> str:
    with session_scope() as session:
        row = session.get(MonitorState, key)
        return row.value if row else ""


def set_monitor_state(key: str, value: str) -> None:
    with session_scope() as session:
        row = session.get(MonitorState, key)
        if row is None:
            session.add(MonitorState(key=key, value=value))
        else:
            row.value = value


def enqueue_messages(messages: Iterable[dict[str, str]], workflow_run_id: str) -> int:
    added = 0
    with session_scope() as session:
        for item in messages:
            message_id = str(item.get("id") or "").strip()
            thread_id = str(item.get("threadId") or "").strip()
            if not message_id:
                continue
            message = session.get(GmailMessage, message_id)
            if message is None:
                message = GmailMessage(
                    message_id=message_id,
                    thread_id=thread_id or message_id,
                    subject="(수집 대기)",
                )
                session.add(message)
                session.flush()
            pending = session.get(GmailPendingMessage, message_id)
            if pending is None:
                session.add(
                    GmailPendingMessage(
                        message_id=message_id,
                        thread_id=thread_id or message.thread_id,
                        priority=100,
                        status="PENDING",
                    )
                )
                added += 1
            event = session.scalar(
                select(MailEvent).where(MailEvent.gmail_message_id == message_id)
            )
            if event is None:
                session.add(
                    MailEvent(
                        gmail_message_id=message_id,
                        workflow_run_id=workflow_run_id,
                        status="QUEUED",
                        current_node="enqueue_message",
                    )
                )
    return added


def list_ready_messages(limit: int) -> list[dict[str, str | int]]:
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.scalars(
            select(GmailPendingMessage)
            .where(
                GmailPendingMessage.status.in_(["PENDING", "RETRY_WAIT"]),
                or_(
                    GmailPendingMessage.next_attempt_at_utc.is_(None),
                    GmailPendingMessage.next_attempt_at_utc <= now,
                ),
            )
            .order_by(
                GmailPendingMessage.priority.desc(),
                GmailPendingMessage.queued_at_utc.asc(),
            )
            .limit(limit)
        ).all()
        return [
            {
                "message_id": row.message_id,
                "thread_id": row.thread_id,
                "attempt_count": row.attempt_count,
            }
            for row in rows
        ]


def get_mail_event(message_id: str, workflow_run_id: str) -> MailEvent:
    with session_scope() as session:
        event = session.scalar(select(MailEvent).where(MailEvent.gmail_message_id == message_id))
        if event is None:
            raise LookupError(f"mail event not found: {message_id}")
        event.workflow_run_id = workflow_run_id
        session.flush()
        session.expunge(event)
        return event


def update_event(event_id: str, *, status: str, current_node: str) -> None:
    with session_scope() as session:
        event = session.get(MailEvent, event_id)
        if event is not None:
            event.status = status
            event.current_node = current_node
            event.updated_at_utc = datetime.now(UTC)


def mark_processing(message_id: str) -> None:
    with session_scope() as session:
        pending = session.get(GmailPendingMessage, message_id)
        if pending is not None:
            pending.status = "PROCESSING"
            pending.last_error = None


def mark_completed(message_id: str, event_id: str) -> None:
    with session_scope() as session:
        pending = session.get(GmailPendingMessage, message_id)
        event = session.get(MailEvent, event_id)
        if pending is not None:
            pending.status = "COMPLETED"
            pending.next_attempt_at_utc = None
            pending.last_error = None
        if event is not None:
            event.status = "EXTRACTED"
            event.current_node = "extract_evidence"


def finalize_message(
    message_id: str,
    event_id: str,
    *,
    event_status: str = "COMPLETED",
) -> None:
    with session_scope() as session:
        pending = session.get(GmailPendingMessage, message_id)
        event = session.get(MailEvent, event_id)
        if pending is not None:
            pending.status = "COMPLETED"
            pending.next_attempt_at_utc = None
            pending.last_error = None
        if event is not None:
            event.status = event_status
            event.current_node = "finalize_case"
            event.updated_at_utc = datetime.now(UTC)


def mark_excluded(message_id: str, event_id: str, reason: str) -> None:
    with session_scope() as session:
        pending = session.get(GmailPendingMessage, message_id)
        event = session.get(MailEvent, event_id)
        message = session.get(GmailMessage, message_id)
        if pending is not None:
            pending.status = "EXCLUDED"
            pending.last_error = reason
        if event is not None:
            event.status = "EXCLUDED"
            event.current_node = "validate_scope"
        if message is not None:
            message.exclusion_reason = reason


def mark_failed(message_id: str, event_id: str, error: Exception) -> None:
    with session_scope() as session:
        pending = session.get(GmailPendingMessage, message_id)
        event = session.get(MailEvent, event_id)
        if pending is not None:
            pending.attempt_count += 1
            delay = min(3600, 60 * (2 ** min(pending.attempt_count - 1, 6)))
            pending.status = "RETRY_WAIT"
            pending.last_error = f"{type(error).__name__}: {error}"[:4000]
            pending.next_attempt_at_utc = datetime.now(UTC) + timedelta(seconds=delay)
        if event is not None:
            event.status = "RETRY_WAIT"


def persist_message(parsed: ParsedMessage, paths: dict[str, str]) -> None:
    with session_scope() as session:
        row = session.get(GmailMessage, parsed.message_id)
        if row is None:
            row = GmailMessage(message_id=parsed.message_id, thread_id=parsed.thread_id)
            session.add(row)
        row.thread_id = parsed.thread_id
        row.history_id = parsed.history_id or None
        row.internal_date_utc = parsed.internal_date_utc
        row.sender = parsed.sender
        row.sender_email = parsed.sender_email
        row.recipients_json = json.dumps(parsed.recipients, ensure_ascii=False)
        row.cc_json = json.dumps(parsed.cc, ensure_ascii=False)
        row.subject = parsed.subject
        row.labels_json = json.dumps(parsed.labels, ensure_ascii=False)
        row.body_text_path = paths.get("body_text_path")
        row.body_html_path = paths.get("body_html_path")
        row.actual_body_path = paths.get("actual_body_path")
        row.quoted_body_path = paths.get("quoted_body_path")
        row.raw_message_path = paths.get("raw_message_path")
        row.content_sha256 = paths.get("content_sha256")
        row.collected_at_utc = datetime.now(UTC)


def messages_for_thread(thread_id: str) -> list[GmailMessage]:
    with session_scope() as session:
        rows = session.scalars(
            select(GmailMessage)
            .where(GmailMessage.thread_id == thread_id)
            .order_by(GmailMessage.internal_date_utc.asc())
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def upsert_attachment(attachment: StoredAttachment) -> None:
    with session_scope() as session:
        row = session.scalar(
            select(MailAttachment).where(
                MailAttachment.gmail_message_id == attachment.gmail_message_id,
                MailAttachment.gmail_attachment_id == attachment.gmail_attachment_id,
                MailAttachment.file_name == attachment.file_name,
            )
        )
        if row is None:
            row = MailAttachment(
                gmail_message_id=attachment.gmail_message_id,
                gmail_attachment_id=attachment.gmail_attachment_id,
                file_name=attachment.file_name,
            )
            session.add(row)
        row.mime_type = attachment.mime_type
        row.size_bytes = attachment.size_bytes
        row.sha256 = attachment.sha256 or None
        row.storage_path = attachment.storage_path or None
        row.extraction_status = attachment.extraction_status
        row.extracted_text_path = attachment.extracted_text_path
        row.ocr_text_path = attachment.ocr_text_path
        row.warning_json = json.dumps(attachment.warnings, ensure_ascii=False)


def add_evidence_snapshot(
    event_id: str,
    *,
    evidence_type: str,
    source_key: str,
    storage_path: str | None,
    payload: Any,
    digest: str,
) -> None:
    with session_scope() as session:
        existing = session.scalar(
            select(EvidenceSnapshot).where(
                EvidenceSnapshot.mail_event_id == event_id,
                EvidenceSnapshot.evidence_type == evidence_type,
                EvidenceSnapshot.source_key == source_key,
                EvidenceSnapshot.sha256 == digest,
            )
        )
        if existing is None:
            session.add(
                EvidenceSnapshot(
                    mail_event_id=event_id,
                    evidence_type=evidence_type,
                    source_key=source_key,
                    storage_path=storage_path,
                    payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                    sha256=digest,
                )
            )


def queue_counts() -> dict[str, int]:
    with session_scope() as session:
        rows = session.scalars(select(GmailPendingMessage)).all()
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.status] = counts.get(row.status, 0) + 1
        return counts
