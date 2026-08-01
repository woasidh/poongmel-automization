from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
import json
import logging
from typing import Any

from prefect import flow, task
from prefect.context import get_run_context

from pungmail.adapters.gmail import GmailHistoryExpiredError, GmailReadOnlyClient
from pungmail.adapters.gmail.evidence import EvidenceStore, MessageEvidenceCollector
from pungmail.adapters.gmail.parser import hard_exclusion_reason
from pungmail.adapters.openai_decision import evidence_bundle_sha256
from pungmail.config import get_settings
from pungmail.domain.decisions import Category, MailDecision
from pungmail.observability.logging import configure_logging
from pungmail.repositories.mail_store import (
    add_evidence_snapshot,
    enqueue_messages,
    get_mail_event,
    get_monitor_state,
    list_ready_messages,
    mark_completed,
    mark_excluded,
    mark_failed,
    finalize_message,
    mark_processing,
    messages_for_thread,
    persist_message,
    set_monitor_state,
    update_event,
    upsert_attachment,
)
from pungmail.repositories.cases import apply_decision
from pungmail.services.cards import render_case
from pungmail.services.decision_pipeline import (
    build_ai_evidence,
    catalog_candidates_for_evidence,
    decide_mail,
    failure_hold_decision,
)
from pungmail.services.outbox import process_outbox_item, queue_card
from pungmail.repositories.tracking import (
    TrackedNode,
    create_workflow_run,
    finish_workflow_run,
    record_skipped_node,
)
from pungmail.workflows.graph import FUTURE_MAIL_NODES


LOGGER = logging.getLogger(__name__)
HISTORY_STATE_KEY = "gmail_history_id"


def _prefect_flow_run_id() -> str | None:
    try:
        return str(get_run_context().flow_run.id)
    except Exception:
        return None


def _prefect_task_run_id() -> str | None:
    try:
        return str(get_run_context().task_run.id)
    except Exception:
        return None


@task(name="Gmail 증분 조회", retries=2, retry_delay_seconds=[5, 15])
def discover_messages_task(workflow_run_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not settings.gmail_enabled:
        record_skipped_node(
            workflow_run_id,
            "discover_gmail",
            reason="PUNGMAIL_GMAIL_ENABLED=false",
        )
        record_skipped_node(
            workflow_run_id,
            "enqueue_message",
            reason="Gmail 수집 비활성화",
        )
        return {"discovered": 0, "queued": 0, "disabled": True}

    gmail = GmailReadOnlyClient(settings)
    with TrackedNode(
        workflow_run_id,
        "discover_gmail",
        input_summary={"history_page_size": settings.history_page_size},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        cursor = get_monitor_state(HISTORY_STATE_KEY)
        if not cursor:
            current = gmail.current_history_id()
            set_monitor_state(HISTORY_STATE_KEY, current)
            node.set_output({"initialized_cursor": True, "history_id": current})
            discovered: list[dict[str, str]] = []
            final_cursor = current
        else:
            discovered = []
            final_cursor = cursor
            try:
                page_token = ""
                while True:
                    page = gmail.list_history_messages(
                        cursor,
                        page_token=page_token,
                        max_results=settings.history_page_size,
                    )
                    discovered.extend(page.messages)
                    final_cursor = page.history_id or final_cursor
                    page_token = page.next_page_token
                    if not page_token:
                        break
            except GmailHistoryExpiredError:
                page_token = ""
                pages = 0
                while pages < 10:
                    page = gmail.list_recovery_messages(
                        page_token=page_token,
                        max_results=settings.history_page_size,
                        newer_than_days=settings.history_recovery_days,
                    )
                    discovered.extend(page.messages)
                    page_token = page.next_page_token
                    pages += 1
                    if not page_token:
                        break
                final_cursor = gmail.current_history_id()
            node.set_output(
                {
                    "discovered": len(discovered),
                    "history_id": final_cursor,
                }
            )

    with TrackedNode(
        workflow_run_id,
        "enqueue_message",
        input_summary={"message_count": len(discovered)},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        queued = enqueue_messages(discovered, workflow_run_id)
        set_monitor_state(HISTORY_STATE_KEY, final_cursor)
        node.set_output({"queued": queued, "deduplicated": len(discovered) - queued})
    return {"discovered": len(discovered), "queued": queued, "disabled": False}


@task(name="대상 메일 확인", retries=1, retry_delay_seconds=5)
def validate_message_task(workflow_run_id: str, message_id: str) -> dict[str, Any]:
    settings = get_settings()
    event = get_mail_event(message_id, workflow_run_id)
    mark_processing(message_id)
    update_event(event.id, status="RUNNING", current_node="validate_scope")
    gmail = GmailReadOnlyClient(settings)
    collector = MessageEvidenceCollector(gmail, settings)
    with TrackedNode(
        workflow_run_id,
        "validate_scope",
        mail_event_id=event.id,
        input_summary={"gmail_message_id": message_id},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        raw = gmail.get_raw_message(message_id)
        parsed, paths = collector.store_raw_message(raw)
        persist_message(parsed, paths)
        reason = hard_exclusion_reason(parsed)
        node.set_output(
            {
                "eligible": reason is None,
                "exclusion_reason": reason,
                "thread_id": parsed.thread_id,
                "subject": parsed.subject,
            }
        )
        if reason:
            mark_excluded(message_id, event.id, reason)
        return {
            "eligible": reason is None,
            "reason": reason,
            "thread_id": parsed.thread_id,
            "event_id": event.id,
        }


@task(name="전체 스레드 수집", retries=1, retry_delay_seconds=5)
def collect_thread_task(
    workflow_run_id: str,
    message_id: str,
    thread_id: str,
    event_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    update_event(event_id, status="RUNNING", current_node="collect_thread")
    gmail = GmailReadOnlyClient(settings)
    collector = MessageEvidenceCollector(gmail, settings)
    with TrackedNode(
        workflow_run_id,
        "collect_thread",
        mail_event_id=event_id,
        input_summary={"gmail_message_id": message_id, "thread_id": thread_id},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        raw_messages = gmail.get_raw_thread(thread_id)
        manifest: list[dict[str, Any]] = []
        for raw in raw_messages:
            parsed, paths = collector.store_raw_message(raw)
            persist_message(parsed, paths)
            manifest.append(
                {
                    "message_id": parsed.message_id,
                    "thread_id": parsed.thread_id,
                    "internal_date_utc": parsed.internal_date_utc,
                    "subject": parsed.subject,
                    "raw_message_path": paths["raw_message_path"],
                    "content_sha256": paths["content_sha256"],
                }
            )
        message_dir = collector.store.message_dir(thread_id, message_id)
        manifest_path, digest = collector.store.write_json(
            message_dir / "thread-manifest.json", manifest
        )
        add_evidence_snapshot(
            event_id,
            evidence_type="GMAIL_THREAD",
            source_key=thread_id,
            storage_path=manifest_path,
            payload={"message_count": len(manifest)},
            digest=digest,
        )
        node.set_output(
            {
                "thread_id": thread_id,
                "message_count": len(manifest),
                "manifest_sha256": digest,
            }
        )
        return {"message_count": len(manifest), "manifest_sha256": digest}


@task(name="본문·첨부·OCR 근거 추출", retries=1, retry_delay_seconds=5)
def extract_evidence_task(
    workflow_run_id: str,
    message_id: str,
    thread_id: str,
    event_id: str,
) -> dict[str, Any]:
    settings = get_settings()
    update_event(event_id, status="RUNNING", current_node="extract_evidence")
    gmail = GmailReadOnlyClient(settings)
    collector = MessageEvidenceCollector(gmail, settings)
    store = EvidenceStore(settings)
    with TrackedNode(
        workflow_run_id,
        "extract_evidence",
        mail_event_id=event_id,
        input_summary={"gmail_message_id": message_id, "thread_id": thread_id},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        attachments = []
        for message in messages_for_thread(thread_id):
            raw_path = store.resolve(message.raw_message_path)
            if raw_path is None or not raw_path.exists():
                raise FileNotFoundError(f"raw message evidence missing: {message.message_id}")
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            for attachment in collector.extract_attachments(raw):
                upsert_attachment(attachment)
                attachments.append(attachment)
        attachment_manifest = [
            {
                "message_id": item.gmail_message_id,
                "attachment_id": item.gmail_attachment_id,
                "file_name": item.file_name,
                "status": item.extraction_status,
                "sha256": item.sha256,
                "warnings": item.warnings,
            }
            for item in attachments
        ]
        manifest_path, digest = store.write_json(
            store.message_dir(thread_id, message_id) / "attachment-manifest.json",
            attachment_manifest,
        )
        add_evidence_snapshot(
            event_id,
            evidence_type="ATTACHMENT_BUNDLE",
            source_key=message_id,
            storage_path=manifest_path,
            payload={
                "attachment_count": len(attachments),
                "failed_count": sum(
                    1 for item in attachments if item.extraction_status == "READ_FAILED"
                ),
            },
            digest=digest,
        )
        node.set_output(
            {
                "attachment_count": len(attachments),
                "ocr_count": sum(1 for item in attachments if item.ocr_text_path),
                "failed_count": sum(
                    1 for item in attachments if item.extraction_status == "READ_FAILED"
                ),
                "manifest_sha256": digest,
            }
        )
        return {
            "attachment_count": len(attachments),
            "ocr_count": sum(1 for item in attachments if item.ocr_text_path),
        }


@task(name="회사 품목 후보 조회")
def lookup_catalog_task(workflow_run_id: str, event_id: str) -> dict[str, Any]:
    evidence = build_ai_evidence(event_id)
    with TrackedNode(
        workflow_run_id,
        "lookup_catalog",
        mail_event_id=event_id,
        input_summary={"evidence_sha256": evidence_bundle_sha256(evidence)},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        candidates = catalog_candidates_for_evidence(evidence)
        node.set_output(
            {
                "candidate_count": len(candidates),
                "ambiguous_names": sorted(
                    {
                        str(item["company_display_name"])
                        for item in candidates
                        if sum(
                            1
                            for other in candidates
                            if other["company_display_name"]
                            == item["company_display_name"]
                        )
                        > 1
                    }
                ),
            }
        )
        return {"candidates": candidates, "evidence_sha256": evidence_bundle_sha256(evidence)}


@task(name="AI 분류·업무값 추출")
def classify_task(
    workflow_run_id: str,
    event_id: str,
    candidates: list[dict[str, object]],
) -> dict[str, Any]:
    settings = get_settings()
    evidence = build_ai_evidence(event_id)
    with TrackedNode(
        workflow_run_id,
        "classify_and_extract",
        mail_event_id=event_id,
        input_summary={
            "model": settings.openai_model,
            "candidate_count": len(candidates),
            "evidence_sha256": evidence_bundle_sha256(evidence),
        },
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        decision, decision_id, fallback_error = decide_mail(
            event_id, evidence, candidates, settings=settings
        )
        node.set_output(
            {
                "ai_decision_id": decision_id,
                "category": decision.category.value,
                "case_action_proposal": decision.case_action.value,
                "fallback_to_hold": fallback_error is not None,
                "failure": fallback_error,
            }
        )
        return {
            "decision": decision.model_dump(mode="json"),
            "ai_decision_id": decision_id,
            "fallback_error": fallback_error,
        }


@task(name="기존 업무 찾기 또는 신규 업무 생성")
def resolve_case_task(
    workflow_run_id: str,
    event_id: str,
    decision_data: dict[str, Any],
    ai_decision_id: str | None,
) -> dict[str, Any]:
    decision = MailDecision.model_validate(decision_data)
    with TrackedNode(
        workflow_run_id,
        "resolve_case",
        mail_event_id=event_id,
        input_summary={
            "category": decision.category.value,
            "ai_case_action": decision.case_action.value,
        },
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        applied = apply_decision(
            event_id, decision, ai_decision_id=ai_decision_id
        )
        output = {
            "business_case_id": applied.business_case_id,
            "category": applied.category.value,
            "event_action": applied.event_action,
            "revision": applied.revision,
            "status": applied.status,
            "ambiguous_case_ids": list(applied.ambiguous_case_ids),
        }
        node.set_output(output)
        return output


BRANCH_NODES: dict[Category, tuple[str, ...]] = {
    Category.ORDER: ("process_order_sources", "check_order_sheet_stock"),
    Category.UPSTREAM_ORDER: ("persist_rw_si", "calculate_upstream_order_status"),
    Category.SAMPLE_DOCUMENT_QUOTE: ("process_sample_document_quote",),
    Category.PUNGLIM_DOCUMENT: ("process_punglim_document_request",),
    Category.INTERNAL_WORK: ("process_internal_work",),
    Category.OVERSEAS_WORK: ("process_overseas_work",),
    Category.HOLD: ("process_hold",),
}


@task(name="카테고리별 처리 분기")
def route_category_task(
    workflow_run_id: str,
    event_id: str,
    case_result: dict[str, Any],
) -> None:
    category = Category(case_result["category"])
    selected = BRANCH_NODES[category]
    with TrackedNode(
        workflow_run_id,
        "route_category",
        mail_event_id=event_id,
        input_summary={"category": category.value},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        node.set_output({"selected_branch": category.value, "nodes": selected})
    for branch, node_keys in BRANCH_NODES.items():
        for index, node_key in enumerate(node_keys):
            if branch != category:
                record_skipped_node(
                    workflow_run_id,
                    node_key,
                    mail_event_id=event_id,
                    branch_key=branch.value,
                    reason=f"선택된 카테고리: {category.value}",
                )
            elif category == Category.ORDER and index == 1:
                record_skipped_node(
                    workflow_run_id,
                    node_key,
                    mail_event_id=event_id,
                    branch_key=branch.value,
                    reason="3단계에서 오더시트·재고표 확인 연결",
                )
            elif category == Category.UPSTREAM_ORDER and index == 1:
                record_skipped_node(
                    workflow_run_id,
                    node_key,
                    mail_event_id=event_id,
                    branch_key=branch.value,
                    reason="4단계에서 RW·SI 진행 상태 계산 연결",
                )
            else:
                with TrackedNode(
                    workflow_run_id,
                    node_key,
                    mail_event_id=event_id,
                    branch_key=branch.value,
                    input_summary={"business_case_id": case_result["business_case_id"]},
                ) as branch_node:
                    branch_node.set_output(
                        {
                            "category": category.value,
                            "status": case_result["status"],
                            "mode": (
                                "STRUCTURED_PREVIEW"
                                if category in (Category.ORDER, Category.UPSTREAM_ORDER)
                                else "APPLIED"
                            ),
                        }
                    )


@task(name="Discord 카드 만들기")
def render_card_task(
    workflow_run_id: str,
    event_id: str,
    business_case_id: str,
) -> dict[str, Any]:
    with TrackedNode(
        workflow_run_id,
        "render_discord",
        mail_event_id=event_id,
        input_summary={"business_case_id": business_case_id},
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        card = render_case(business_case_id)
        output = {
            "business_case_id": card.business_case_id,
            "channel_key": card.channel_key,
            "body_path": card.body_path,
            "body_sha256": card.body_sha256,
            "full_detail_path": card.full_detail_path,
        }
        node.set_output(output)
        return output


@task(name="Discord 알림 반영")
def dispatch_outbox_task(
    workflow_run_id: str,
    event_id: str,
    message_id: str,
    card_data: dict[str, Any],
) -> dict[str, Any]:
    settings = get_settings()
    from pungmail.services.cards import RenderedCard

    card = RenderedCard(
        business_case_id=card_data["business_case_id"],
        channel_key=card_data["channel_key"],
        body="",
        body_path=card_data["body_path"],
        body_sha256=card_data["body_sha256"],
        full_detail_path=card_data.get("full_detail_path"),
    )
    with TrackedNode(
        workflow_run_id,
        "dispatch_outbox",
        mail_event_id=event_id,
        input_summary={
            "channel_key": card.channel_key,
            "mode": settings.discord_mode,
        },
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        outbox_id = queue_card(
            card, source_gmail_message_id=message_id, settings=settings
        )
        status = "PREVIEWED"
        if settings.discord_mode.upper() == "LIVE_TEST":
            status = process_outbox_item(outbox_id, settings=settings)
        output = {"outbox_id": outbox_id, "status": status, "mode": settings.discord_mode}
        node.set_output(output)
        return output


@task(name="결과·상태·이력 확정")
def finalize_case_task(
    workflow_run_id: str,
    event_id: str,
    message_id: str,
    case_result: dict[str, Any],
    outbox_result: dict[str, Any],
) -> None:
    with TrackedNode(
        workflow_run_id,
        "finalize_case",
        mail_event_id=event_id,
        input_summary={
            "business_case_id": case_result["business_case_id"],
            "outbox_id": outbox_result["outbox_id"],
        },
        prefect_task_run_id=_prefect_task_run_id(),
    ) as node:
        final_status = "HOLD" if case_result["category"] == Category.HOLD.value else "COMPLETED"
        finalize_message(message_id, event_id, event_status=final_status)
        node.set_output({"mail_status": final_status, "case_status": case_result["status"]})


def _skip_nodes(
    workflow_run_id: str,
    event_id: str,
    *,
    reason: str,
    include_collection: bool = False,
) -> None:
    if include_collection:
        for key in ("collect_thread", "extract_evidence"):
            record_skipped_node(
                workflow_run_id,
                key,
                mail_event_id=event_id,
                reason=reason,
            )
    for graph_node in FUTURE_MAIL_NODES:
        record_skipped_node(
            workflow_run_id,
            graph_node.key,
            mail_event_id=event_id,
            branch_key=graph_node.branch,
            reason=reason,
        )


@flow(name="mail_processing_issue1_legacy", log_prints=True)
def _mail_processing_issue1(trigger_type: str = "SCHEDULED") -> dict[str, Any]:
    configure_logging()
    run_id = create_workflow_run(
        "mail_processing",
        trigger_type=trigger_type,
        prefect_flow_run_id=_prefect_flow_run_id(),
    )
    summary: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "processed": 0,
        "excluded": 0,
        "failed": 0,
    }
    try:
        discovery = discover_messages_task(run_id)
        summary["discovery"] = discovery
        settings = get_settings()
        for pending in list_ready_messages(settings.max_messages_per_run):
            message_id = str(pending["message_id"])
            event = get_mail_event(message_id, run_id)
            try:
                validation = validate_message_task(run_id, message_id)
                if not validation["eligible"]:
                    _skip_nodes(
                        run_id,
                        event.id,
                        reason=f"수집 제외: {validation['reason']}",
                        include_collection=True,
                    )
                    summary["excluded"] += 1
                    continue
                collect_thread_task(
                    run_id,
                    message_id,
                    str(validation["thread_id"]),
                    event.id,
                )
                extract_evidence_task(
                    run_id,
                    message_id,
                    str(validation["thread_id"]),
                    event.id,
                )
                _skip_nodes(
                    run_id,
                    event.id,
                    reason="이슈 2 이후 구현 범위",
                )
                summary["processed"] += 1
            except Exception as exc:
                LOGGER.exception(
                    "Mail processing failed",
                    extra={
                        "service": "mail-worker",
                        "workflow_run_id": run_id,
                        "gmail_message_id": message_id,
                        "mail_event_id": event.id,
                    },
                )
                mark_failed(message_id, event.id, exc)
                summary["failed"] += 1
        status = "PARTIAL" if summary["failed"] else "SUCCEEDED"
        summary["finished_at"] = datetime.now(UTC).isoformat()
        finish_workflow_run(run_id, status, summary)
        return summary
    except Exception as exc:
        summary["fatal_error"] = f"{type(exc).__name__}: {exc}"
        summary["finished_at"] = datetime.now(UTC).isoformat()
        finish_workflow_run(run_id, "FAILED", summary)
        raise


def _complete_scope_hold(
    run_id: str,
    event_id: str,
    message_id: str,
    reason: str,
) -> None:
    for node_key in (
        "collect_thread",
        "extract_evidence",
        "lookup_catalog",
        "classify_and_extract",
    ):
        record_skipped_node(
            run_id,
            node_key,
            mail_event_id=event_id,
            reason=f"대상 조건 불일치: {reason}",
        )
    evidence = build_ai_evidence(event_id)
    hold = failure_hold_decision(
        evidence,
        RuntimeError(reason),
        failure_node="validate_scope",
    )
    case_result = resolve_case_task(
        run_id, event_id, hold.model_dump(mode="json"), None
    )
    route_category_task(run_id, event_id, case_result)
    card = render_card_task(run_id, event_id, case_result["business_case_id"])
    outbox = dispatch_outbox_task(run_id, event_id, message_id, card)
    finalize_case_task(run_id, event_id, message_id, case_result, outbox)


def _complete_valid_mail(
    run_id: str,
    event_id: str,
    message_id: str,
    thread_id: str,
) -> None:
    collect_thread_task(run_id, message_id, thread_id, event_id)
    extract_evidence_task(run_id, message_id, thread_id, event_id)
    catalog_result = lookup_catalog_task(run_id, event_id)
    classified = classify_task(run_id, event_id, catalog_result["candidates"])
    case_result = resolve_case_task(
        run_id,
        event_id,
        classified["decision"],
        classified["ai_decision_id"],
    )
    route_category_task(run_id, event_id, case_result)
    card = render_card_task(run_id, event_id, case_result["business_case_id"])
    outbox = dispatch_outbox_task(run_id, event_id, message_id, card)
    finalize_case_task(run_id, event_id, message_id, case_result, outbox)


@flow(name="mail_processing", log_prints=True)
def mail_processing(trigger_type: str = "SCHEDULED") -> dict[str, Any]:
    configure_logging()
    run_id = create_workflow_run(
        "mail_processing",
        trigger_type=trigger_type,
        prefect_flow_run_id=_prefect_flow_run_id(),
    )
    summary: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "processed": 0,
        "held": 0,
        "failed": 0,
    }
    try:
        summary["discovery"] = discover_messages_task(run_id)
        settings = get_settings()
        for pending in list_ready_messages(settings.max_messages_per_run):
            message_id = str(pending["message_id"])
            event = get_mail_event(message_id, run_id)
            try:
                validation = validate_message_task(run_id, message_id)
                if validation["eligible"]:
                    _complete_valid_mail(
                        run_id,
                        event.id,
                        message_id,
                        str(validation["thread_id"]),
                    )
                    summary["processed"] += 1
                else:
                    _complete_scope_hold(
                        run_id,
                        event.id,
                        message_id,
                        str(validation["reason"]),
                    )
                    summary["held"] += 1
            except Exception as exc:
                LOGGER.exception(
                    "Mail processing failed",
                    extra={
                        "service": "mail-worker",
                        "workflow_run_id": run_id,
                        "gmail_message_id": message_id,
                        "mail_event_id": event.id,
                    },
                )
                latest = get_mail_event(message_id, run_id)
                if latest.business_case_id is None:
                    try:
                        evidence = build_ai_evidence(event.id)
                        hold = failure_hold_decision(evidence, exc)
                        case_result = resolve_case_task(
                            run_id, event.id, hold.model_dump(mode="json"), None
                        )
                        route_category_task(run_id, event.id, case_result)
                        card = render_card_task(
                            run_id, event.id, case_result["business_case_id"]
                        )
                        outbox = dispatch_outbox_task(
                            run_id, event.id, message_id, card
                        )
                        finalize_case_task(
                            run_id, event.id, message_id, case_result, outbox
                        )
                        summary["held"] += 1
                        continue
                    except Exception:
                        LOGGER.exception("Hold fallback also failed")
                mark_failed(message_id, event.id, exc)
                summary["failed"] += 1
        status = "PARTIAL" if summary["failed"] else "SUCCEEDED"
        summary["finished_at"] = datetime.now(UTC).isoformat()
        finish_workflow_run(run_id, status, summary)
        return summary
    except Exception as exc:
        summary["fatal_error"] = f"{type(exc).__name__}: {exc}"
        summary["finished_at"] = datetime.now(UTC).isoformat()
        finish_workflow_run(run_id, "FAILED", summary)
        raise
