from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import select

from pungmail.adapters.catalog import CompanyCatalog
from pungmail.adapters.openai_decision import OpenAIMailDecisionClient
from pungmail.config import Settings, get_settings
from pungmail.domain.decisions import (
    Category,
    MailDecision,
    OrderPayload,
    PunglimDocumentRequestPayload,
    SampleDocumentQuotePayload,
    UpstreamOrderPayload,
)
from pungmail.prompts import build_prompt_bundle
from pungmail.repositories.ai_store import save_ai_failure, save_ai_success
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    GmailMessage,
    MailAttachment,
    MailEvent,
)


AI_ATTACHMENT_TEXT_LIMIT = 12_000
RICHWOOD_DOMAIN = "richwood.net"


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


def _attachment_text(
    settings: Settings,
    stored_path: str | None,
) -> tuple[str, bool]:
    value = _read(settings, stored_path, limit=AI_ATTACHMENT_TEXT_LIMIT + 1)
    return value[:AI_ATTACHMENT_TEXT_LIMIT], len(value) > AI_ATTACHMENT_TEXT_LIMIT


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
        attachment_evidence: list[dict[str, Any]] = []
        for row in attachments:
            extracted_text, extracted_truncated = _attachment_text(
                active, row.extracted_text_path
            )
            ocr_text, ocr_truncated = _attachment_text(active, row.ocr_text_path)
            attachment_evidence.append(
                {
                    "ref": f"attachment:{row.id}",
                    "message_id": row.gmail_message_id,
                    "file_name": row.file_name,
                    "mime_type": row.mime_type,
                    "extraction_status": row.extraction_status,
                    "extracted_text": extracted_text,
                    "ocr_text": ocr_text,
                    "text_truncated_for_ai": extracted_truncated or ocr_truncated,
                    "ai_text_limit_chars": AI_ATTACHMENT_TEXT_LIMIT,
                    "warnings": json.loads(row.warning_json),
                }
            )
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
            "attachments": attachment_evidence,
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


def _upstream_rw_numbers(
    decision: MailDecision,
    evidence: dict[str, Any],
) -> list[str]:
    values = [*decision.case_lookup_keys.rw_numbers]
    payload = decision.category_payload
    if isinstance(payload, OrderPayload):
        values.extend(payload.po_numbers)
    elif isinstance(payload, UpstreamOrderPayload):
        values.extend(payload.rw_numbers)
    text = "\n".join(
        str(message.get(key) or "")
        for message in evidence.get("messages", [])
        for key in ("subject", "actual_body")
    )
    text += "\n" + "\n".join(
        str(attachment.get("extracted_text") or "")
        for attachment in evidence.get("attachments", [])
    )
    values.extend(f"RW-{number}" for number in re.findall(r"\bRW\s*[-_]?\s*(\d{3,})\b", text, re.I))
    values.extend(
        f"RW-{number}"
        for number in re.findall(r"\[RICHWOOD\].*?\bPO\s*[-_]?\s*(\d{3,})\b", text, re.I)
    )
    normalized: list[str] = []
    for value in values:
        match = re.search(r"(?:RW|PO)\s*[-_]?\s*(\d{3,})", value, re.I)
        candidate = f"RW-{match.group(1)}" if match else value.strip()
        if candidate and candidate.casefold() not in {item.casefold() for item in normalized}:
            normalized.append(candidate)
    return normalized


def apply_direction_guards(
    decision: MailDecision,
    evidence: dict[str, Any],
) -> MailDecision:
    """명확한 발신 방향 근거가 AI 분류와 충돌하면 업무 방향을 보정한다."""
    messages = evidence.get("messages", [])
    if not messages:
        return decision
    first = messages[0]
    sender = str(first.get("sender_email") or "").casefold()
    recipients = [
        str(value).casefold()
        for value in [*(first.get("recipients") or []), *(first.get("cc") or [])]
    ]
    sender_is_richwood = sender.endswith(f"@{RICHWOOD_DOMAIN}")
    has_richwood_recipient = any(
        address.endswith(f"@{RICHWOOD_DOMAIN}") for address in recipients
    )
    has_external_recipient = any(
        "@" in address and not address.endswith(f"@{RICHWOOD_DOMAIN}")
        for address in recipients
    )

    if decision.category == Category.UPSTREAM_ORDER and isinstance(
        decision.category_payload, UpstreamOrderPayload
    ):
        rw_numbers = _upstream_rw_numbers(decision, evidence)
        if not rw_numbers:
            return decision
        lookup_keys = decision.case_lookup_keys.model_copy(
            update={"rw_numbers": rw_numbers}
        )
        payload = decision.category_payload.model_copy(
            update={"rw_numbers": rw_numbers}
        )
        return decision.model_copy(
            update={"case_lookup_keys": lookup_keys, "category_payload": payload}
        )

    if decision.category == Category.PUNGLIM_DOCUMENT and isinstance(
        decision.category_payload, PunglimDocumentRequestPayload
    ):
        if sender_is_richwood or not has_richwood_recipient:
            return decision
        payload = decision.category_payload
        customer_payload = SampleDocumentQuotePayload(
            payload_type="SAMPLE_DOCUMENT_QUOTE",
            items=payload.items,
            components=payload.components,
            end_user=None,
            destination=None,
            recipient=payload.recipient,
            contact=payload.contact,
        )
        return decision.model_copy(
            update={
                "category": Category.SAMPLE_DOCUMENT_QUOTE,
                "category_payload": customer_payload,
            }
        )

    if decision.category != Category.ORDER or not isinstance(
        decision.category_payload, OrderPayload
    ):
        return decision
    if not sender_is_richwood:
        return decision
    if not has_external_recipient:
        return decision
    attachment_text = "\n".join(
        str(attachment.get("extracted_text") or "")
        for attachment in evidence.get("attachments", [])
    )
    first_text = "\n".join(
        str(first.get(key) or "") for key in ("subject", "actual_body")
    )
    rw_numbers = _upstream_rw_numbers(decision, evidence)
    explicit_purchase_order = "purchase order" in attachment_text.casefold()
    richwood_is_document_sender = bool(
        re.search(r"\bFROM\s*:\s*RICHWOOD\b", attachment_text, re.I)
    )
    po_request = bool(
        re.search(r"\[RICHWOOD\].*?\bPO\s*[-_]?\s*\d{3,}", first_text, re.I)
    )
    if not rw_numbers or not (po_request or explicit_purchase_order and richwood_is_document_sender):
        return decision

    payload = decision.category_payload
    remaining_po_numbers = [
        value
        for value in payload.po_numbers
        if not re.search(r"(?:RW|PO)\s*[-_]?\s*\d{3,}", value, re.I)
    ]
    lookup_keys = decision.case_lookup_keys.model_copy(
        update={
            "po_numbers": remaining_po_numbers,
            "rw_numbers": rw_numbers,
        }
    )
    upstream_payload = UpstreamOrderPayload(
        payload_type="UPSTREAM_ORDER",
        rw_numbers=rw_numbers,
        si_numbers=decision.case_lookup_keys.si_numbers,
        items=payload.items,
        status_text=None,
        notes=payload.notes,
    )
    return decision.model_copy(
        update={
            "category": Category.UPSTREAM_ORDER,
            "case_lookup_keys": lookup_keys,
            "category_payload": upstream_payload,
        }
    )


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
        result = replace(result, decision=apply_direction_guards(result.decision, evidence))
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
