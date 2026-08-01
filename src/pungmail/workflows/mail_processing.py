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
from pungmail.config import get_settings
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
    mark_processing,
    messages_for_thread,
    persist_message,
    set_monitor_state,
    update_event,
    upsert_attachment,
)
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
        mark_completed(message_id, event_id)
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
