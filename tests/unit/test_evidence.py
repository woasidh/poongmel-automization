from __future__ import annotations

from base64 import urlsafe_b64encode
from types import SimpleNamespace

from pungmail.adapters.gmail.evidence import MessageEvidenceCollector
from pungmail.adapters.legacy.extractor import ExtractionResult


class NoDownloadGmail:
    def download_attachment(self, _message_id: str, _attachment_id: str) -> bytes:
        raise AssertionError("inline attachment must not call Gmail")


class TextExtractor:
    module = SimpleNamespace(_large_attachment_filename=lambda *_args: "large.bin")

    def extract(self, _filename: str, _mime_type: str, data: bytes) -> ExtractionResult:
        return ExtractionResult(text=data.decode("utf-8"), status="READ")


def test_evidence_store_keeps_raw_body_and_extracted_attachment(isolated_settings) -> None:
    body = urlsafe_b64encode("본문".encode()).decode().rstrip("=")
    attachment = urlsafe_b64encode(b"attachment text").decode().rstrip("=")
    raw = {
        "id": "message-2",
        "threadId": "thread-2",
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "From", "value": "user@richwood.net"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": body}},
                {
                    "mimeType": "text/plain",
                    "filename": "evidence.txt",
                    "body": {"data": attachment, "size": 15},
                },
            ],
        },
    }
    collector = MessageEvidenceCollector(
        NoDownloadGmail(), isolated_settings, extractor=TextExtractor()  # type: ignore[arg-type]
    )

    parsed, paths = collector.store_raw_message(raw)
    attachments = collector.extract_attachments(raw)

    assert parsed.actual_body == "본문"
    assert (isolated_settings.project_root / paths["raw_message_path"]).exists()
    assert attachments[0].extraction_status == "READ"
    extracted_path = isolated_settings.project_root / str(attachments[0].extracted_text_path)
    assert extracted_path.read_text(encoding="utf-8") == "attachment text"
