from __future__ import annotations

from base64 import urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha256

from pungmail.adapters.gmail.evidence import MessageEvidenceCollector
from pungmail.adapters.gmail.parser import hard_exclusion_reason
from pungmail.config import get_settings
from pungmail.repositories.mail_store import (
    add_evidence_snapshot,
    enqueue_messages,
    get_mail_event,
    mark_completed,
    persist_message,
    upsert_attachment,
)
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
