from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pungmail.adapters.catalog import CompanyCatalog
from pungmail.adapters.openai_decision import OpenAIMailDecisionClient
from pungmail.config import Settings, get_settings
from pungmail.domain.decisions import MailDecision
from pungmail.prompts import build_prompt_bundle
from pungmail.repositories.ai_store import save_ai_failure, save_ai_success
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    GmailMessage,
    MailAttachment,
    MailEvent,
)


def _read(settings: Settings, stored_path: str | None, limit: int = 200_000) -> str:
    if not stored_path:
        return ""
    path = Path(stored_path)
    if not path.is_absolute():
        path = settings.project_root / path
    resolved = path.resolve()
    allowed = (settings.evidence_path.resolve(), settings.project_root.resolve())
    if not any(root == resolved or root in resolved.parents for root in allowed):
        return ""
    if not resolved.exists():
        return ""
    return resolved.read_text(encoding="utf-8", errors="replace")[:limit]


def build_ai_evidence(
    mail_event_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    active = settings or get_settings()
    with session_scope() as session:
        event = session.get(MailEvent, mail_event_id)
        if event is None:
            raise LookupError(f"mail event not found: {mail_event_id}")
        message = session.get(GmailMessage, event.gmail_message_id)
        if message is None:
            raise LookupError(f"Gmail message not found: {event.gmail_message_id}")
        thread = session.scalars(
            select(GmailMessage)
            .where(GmailMessage.thread_id == message.thread_id)
            .order_by(GmailMessage.internal_date_utc.asc())
        ).all()
        message_ids = [row.message_id for row in thread]
        attachments = session.scalars(
            select(MailAttachment)
            .where(MailAttachment.gmail_message_id.in_(message_ids))
            .order_by(MailAttachment.gmail_message_id, MailAttachment.file_name)
        ).all()
        return {
            "target_message_id": message.message_id,
            "thread_id": message.thread_id,
            "messages": [
                {
                    "ref": f"gmail:{row.message_id}",
                    "message_id": row.message_id,
                    "sent_at": row.internal_date_utc.isoformat() if row.internal_date_utc else None,
                    "sender": row.sender,
                    "sender_email": row.sender_email,
                    "recipients": json.loads(row.recipients_json),
                    "cc": json.loads(row.cc_json),
                    "subject": row.subject,
                    "actual_body": _read(active, row.actual_body_path),
                    "quoted_body": _read(active, row.quoted_body_path),
                }
                for row in thread
            ],
            "attachments": [
                {
                    "ref": f"attachment:{row.id}",
                    "message_id": row.gmail_message_id,
                    "file_name": row.file_name,
                    "mime_type": row.mime_type,
                    "extraction_status": row.extraction_status,
                    "extracted_text": _read(active, row.extracted_text_path),
                    "ocr_text": _read(active, row.ocr_text_path),
                    "warnings": json.loads(row.warning_json),
                }
                for row in attachments
            ],
        }


def catalog_candidates_for_evidence(
    evidence: dict[str, Any],
    *,
    catalog: CompanyCatalog | None = None,
) -> list[dict[str, object]]:
    active_catalog = catalog or CompanyCatalog()
    text_parts: list[str] = []
    for message in evidence.get("messages", []):
        text_parts.extend(
            str(message.get(key) or "")
            for key in ("subject", "actual_body", "quoted_body")
        )
    for attachment in evidence.get("attachments", []):
        text_parts.extend(
            str(attachment.get(key) or "")
            for key in ("file_name", "extracted_text", "ocr_text")
        )
    return [
        candidate.as_dict()
        for candidate in active_catalog.candidates_in_text("\n".join(text_parts))
    ]


def failure_hold_decision(
    evidence: dict[str, Any],
    error: Exception,
    *,
    failure_node: str = "classify_and_extract",
) -> MailDecision:
    target = str(evidence.get("target_message_id") or "unknown")
    thread_id = str(evidence.get("thread_id") or "") or None
    messages = evidence.get("messages") or []
    last = messages[-1] if messages else {}
    return MailDecision.model_validate(
        {
            "category": "보류",
            "case_action": "CREATE",
            "case_lookup_keys": {
                "original_message_id": target,
                "thread_id": thread_id,
                "company_key": None,
                "po_numbers": [],
                "rw_numbers": [],
                "si_numbers": [],
                "item_component_keys": [],
            },
            "company": None,
            "subject": str(last.get("subject") or "분류 실패 메일"),
            "summary": "AI 판정에 실패하여 자동으로 보류했습니다.",
            "missing_fields": ["AI 구조화 판정 결과"],
            "evidence_refs": [f"gmail:{target}"],
            "category_payload": {
                "payload_type": "HOLD",
                "failure_node": failure_node,
                "failure_type": type(error).__name__,
                "unresolved_values": [],
                "check_items": ["실패 원인을 확인한 뒤 재시도하세요."],
                "retryable": True,
            },
        }
    )


def decide_mail(
    mail_event_id: str,
    evidence: dict[str, Any],
    candidates: list[dict[str, object]],
    *,
    settings: Settings | None = None,
    client: OpenAIMailDecisionClient | None = None,
) -> tuple[MailDecision, str, str | None]:
    active = settings or get_settings()
    try:
        result = (client or OpenAIMailDecisionClient(active)).decide(evidence, candidates)
        decision_id = save_ai_success(
            mail_event_id,
            result,
            evidence,
            reasoning_effort=active.openai_reasoning_effort,
        )
        return result.decision, decision_id, None
    except Exception as exc:
        hold = failure_hold_decision(evidence, exc)
        decision_id = save_ai_failure(
            mail_event_id,
            exc,
            evidence,
            build_prompt_bundle(),
            hold,
            reasoning_effort=active.openai_reasoning_effort,
        )
        return hold, decision_id, f"{type(exc).__name__}: {exc}"
