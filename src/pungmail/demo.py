from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha256
import json
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select

from pungmail.adapters.gmail.evidence import MessageEvidenceCollector
from pungmail.adapters.gmail.parser import hard_exclusion_reason
from pungmail.config import get_settings
from pungmail.adapters.openai_decision import DecisionResult
from pungmail.domain.decisions import MailDecision
from pungmail.prompts import (
    PromptStage,
    build_category_prompt_bundle,
    build_classification_prompt_bundle,
    build_prompt_trace,
)
from pungmail.repositories.ai_store import save_ai_success
from pungmail.repositories.cases import apply_decision
from pungmail.repositories.database import session_scope
from pungmail.repositories.mail_store import (
    add_evidence_snapshot,
    enqueue_messages,
    get_mail_event,
    mark_completed,
    finalize_message,
    persist_message,
    upsert_attachment,
)
from pungmail.repositories.models import GmailMessage
from pungmail.services.cards import render_case
from pungmail.services.decision_pipeline import build_ai_evidence, catalog_candidates_for_evidence
from pungmail.services.outbox import queue_card
from pungmail.workflows.mail_processing import route_category_task
from pungmail.repositories.tracking import (
    TrackedNode,
    create_workflow_run,
    finish_workflow_run,
    record_skipped_node,
)
from pungmail.workflows.graph import FUTURE_MAIL_NODES


def _encoded(value: str) -> str:
    return urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


class InlineOnlyGmail:
    def download_attachment(self, _message_id: str, _attachment_id: str) -> bytes:
        raise RuntimeError("demo attachment must be inline")


def seed_demo() -> str:
    settings = get_settings()
    message_id = "demo-mail-001"
    thread_id = "demo-thread-001"
    run_id = create_workflow_run("mail_processing", trigger_type="MANUAL")
    enqueue_messages([{"id": message_id, "threadId": thread_id}], run_id)
    event = get_mail_event(message_id, run_id)
    raw = {
        "id": message_id,
        "threadId": thread_id,
        "historyId": "demo-history",
        "internalDate": str(int(datetime.now(UTC).timestamp() * 1000)),
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "테스트 고객 <customer@example.com>"},
                {"name": "To", "value": "풍림 담당자 <staff@richwood.net>"},
                {"name": "Subject", "value": "[검수용] 차세대 풍멜이 메일 수집"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _encoded("안녕하세요.\n첨부 자료 확인 부탁드립니다.\n\n-----Original Message-----\n이전 대화입니다.")}},
                {"filename": "검수자료.txt", "mimeType": "text/plain", "body": {"data": _encoded("첨부파일 추출이 정상 동작합니다."), "size": 52}},
            ],
        },
    }
    collector = MessageEvidenceCollector(InlineOnlyGmail(), settings)  # type: ignore[arg-type]
    with TrackedNode(run_id, "validate_scope", mail_event_id=event.id) as node:
        parsed, paths = collector.store_raw_message(raw)
        persist_message(parsed, paths)
        reason = hard_exclusion_reason(parsed)
        node.set_output({"eligible": reason is None, "subject": parsed.subject})
    with TrackedNode(run_id, "collect_thread", mail_event_id=event.id) as node:
        manifest_path, digest = collector.store.write_json(
            collector.store.message_dir(thread_id, message_id) / "thread-manifest.json",
            [{"message_id": message_id, "content_sha256": paths["content_sha256"]}],
        )
        add_evidence_snapshot(
            event.id,
            evidence_type="GMAIL_THREAD",
            source_key=thread_id,
            storage_path=manifest_path,
            payload={"message_count": 1},
            digest=digest,
        )
        node.set_output({"message_count": 1, "manifest_sha256": digest})
    with TrackedNode(run_id, "extract_evidence", mail_event_id=event.id) as node:
        attachments = collector.extract_attachments(raw)
        for attachment in attachments:
            upsert_attachment(attachment)
        bundle = "".join(item.sha256 for item in attachments)
        digest = sha256(bundle.encode("ascii")).hexdigest()
        add_evidence_snapshot(
            event.id,
            evidence_type="ATTACHMENT_BUNDLE",
            source_key=message_id,
            storage_path=None,
            payload={"attachment_count": len(attachments)},
            digest=digest,
        )
        node.set_output({"attachment_count": len(attachments)})
    for graph_node in FUTURE_MAIL_NODES:
        record_skipped_node(
            run_id,
            graph_node.key,
            mail_event_id=event.id,
            branch_key=graph_node.branch,
            reason="이슈 2 이후 구현 범위",
        )
    mark_completed(message_id, event.id)
    finish_workflow_run(run_id, "SUCCEEDED", {"demo": True, "processed": 1})
    return message_id


def _issue2_decision(message_id: str, thread_id: str, *, completed: bool) -> MailDecision:
    return MailDecision.model_validate(
        {
            "category": "샘플자료견적",
            "case_action": "UPDATE" if completed else "CREATE",
            "case_lookup_keys": {
                "original_message_id": message_id,
                "thread_id": thread_id,
                "company_key": "demo-customer",
                "po_numbers": [],
                "rw_numbers": [],
                "si_numbers": [],
                "item_component_keys": ["VC-IP|SAMPLE", "VC-IP|DOCUMENT"],
            },
            "company": "데모 고객사",
            "subject": "VC-IP 샘플·자료 요청" if not completed else "VC-IP 샘플·자료 완료 회신",
            "summary": "VC-IP 샘플과 제품 자료를 함께 관리하는 복합 업무",
            "missing_fields": [],
            "evidence_refs": [f"gmail:{message_id}"],
            "category_payload": {
                "payload_type": "SAMPLE_DOCUMENT_QUOTE",
                "items": [
                    {
                        "raw_product_name": "VC-IP",
                        "item_code": "30500314",
                        "spec": "5KG",
                        "quantity": "1",
                        "unit": "EA",
                    }
                ],
                "components": [
                    {
                        "component_type": "SAMPLE",
                        "label": "VC-IP 샘플",
                        "completed": completed,
                        "evidence_ref": f"gmail:{message_id}",
                    },
                    {
                        "component_type": "DOCUMENT",
                        "label": "VC-IP 제품 자료",
                        "completed": completed,
                        "evidence_ref": f"gmail:{message_id}",
                    },
                ],
                "end_user": "데모 최종 사용자",
                "destination": "서울시 데모구",
                "recipient": "홍길동",
                "contact": None,
            },
        }
    )


def seed_issue2_demo() -> str:
    settings = get_settings()
    suffix = uuid4().hex[:8]
    thread_id = f"issue2-demo-thread-{suffix}"
    run_id = create_workflow_run("mail_processing", trigger_type="MANUAL")
    with TrackedNode(run_id, "discover_gmail") as node:
        node.set_output({"discovered": 2, "demo": True})
    with TrackedNode(run_id, "enqueue_message") as node:
        node.set_output({"queued": 2, "demo": True})

    case_id = ""
    for revision, completed in ((1, False), (2, True)):
        message_id = f"issue2-demo-mail-{suffix}-{revision}"
        enqueue_messages([{"id": message_id, "threadId": thread_id}], run_id)
        event = get_mail_event(message_id, run_id)
        body_dir = settings.evidence_path / thread_id / message_id
        body_dir.mkdir(parents=True, exist_ok=True)
        body_path = body_dir / "actual-body.txt"
        body_text = (
            "VC-IP 샘플 1개와 제품 자료를 부탁드립니다."
            if not completed
            else "요청하신 VC-IP 샘플과 제품 자료 전달을 완료했습니다."
        )
        body_path.write_text(body_text, encoding="utf-8")
        with session_scope() as session:
            message = session.get(GmailMessage, message_id)
            assert message is not None
            message.thread_id = thread_id
            message.sender = "데모 고객 <demo@example.com>"
            message.sender_email = "demo@example.com"
            message.recipients_json = json.dumps(["staff@richwood.net"])
            message.subject = (
                "[데모] VC-IP 샘플·자료 요청"
                if not completed
                else "Re: [데모] VC-IP 샘플·자료 요청"
            )
            message.actual_body_path = str(body_path.relative_to(settings.project_root))
            message.body_text_path = message.actual_body_path
            message.internal_date_utc = datetime.now(UTC)

        with TrackedNode(run_id, "validate_scope", mail_event_id=event.id) as node:
            node.set_output({"eligible": True, "demo": True})
        with TrackedNode(run_id, "collect_thread", mail_event_id=event.id) as node:
            node.set_output({"message_count": revision, "thread_id": thread_id})
        with TrackedNode(run_id, "extract_evidence", mail_event_id=event.id) as node:
            node.set_output({"attachment_count": 0, "ocr_count": 0})

        evidence = build_ai_evidence(event.id, settings=settings)
        candidates = catalog_candidates_for_evidence(evidence)
        with TrackedNode(run_id, "lookup_catalog", mail_event_id=event.id) as node:
            node.set_output({"candidate_count": len(candidates), "matched": "VC-IP"})
        decision = _issue2_decision(message_id, thread_id, completed=completed)
        prompt_trace = build_prompt_trace(
            PromptStage(
                name="CATEGORY_CLASSIFICATION",
                bundle=build_classification_prompt_bundle(),
            ),
            PromptStage(
                name="CATEGORY_PROCESSING",
                category=decision.category.value,
                bundle=build_category_prompt_bundle(decision.category),
            ),
        )
        raw_path = settings.ai_response_path / f"demo-response-{suffix}-{revision}.json"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_json = json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, indent=2)
        raw_path.write_text(raw_json, encoding="utf-8")
        result = DecisionResult(
            decision=decision,
            response_id=f"resp_demo_{suffix}_{revision}",
            latency_ms=120 + revision,
            input_tokens=500,
            output_tokens=180,
            raw_response_path=str(raw_path.relative_to(settings.project_root)),
            raw_response_sha256=sha256(raw_json.encode("utf-8")).hexdigest(),
            prompt_trace=prompt_trace,
        )
        decision_id = save_ai_success(
            event.id,
            result,
            evidence,
            reasoning_effort=settings.openai_reasoning_effort,
        )
        with TrackedNode(run_id, "classify_and_extract", mail_event_id=event.id) as node:
            node.set_output(
                {"category": decision.category.value, "ai_decision_id": decision_id}
            )
        with TrackedNode(run_id, "resolve_case", mail_event_id=event.id) as node:
            applied = apply_decision(event.id, decision, ai_decision_id=decision_id)
            case_id = applied.business_case_id
            node.set_output(
                {
                    "business_case_id": case_id,
                    "event_action": applied.event_action,
                    "revision": applied.revision,
                }
            )
        route_category_task.fn(
            run_id,
            event.id,
            {
                "business_case_id": case_id,
                "category": applied.category.value,
                "status": applied.status,
            },
        )
        with TrackedNode(run_id, "render_discord", mail_event_id=event.id) as node:
            card = render_case(case_id, settings=settings)
            node.set_output({"channel_key": card.channel_key, "body_sha256": card.body_sha256})
        with TrackedNode(run_id, "dispatch_outbox", mail_event_id=event.id) as node:
            outbox_id = queue_card(
                card, source_gmail_message_id=message_id, settings=settings
            )
            node.set_output({"outbox_id": outbox_id, "status": "PREVIEWED"})
        with TrackedNode(run_id, "finalize_case", mail_event_id=event.id) as node:
            finalize_message(message_id, event.id)
            node.set_output({"case_status": applied.status})

    finish_workflow_run(
        run_id,
        "SUCCEEDED",
        {"demo": True, "processed": 2, "business_case_id": case_id},
    )
    return case_id
