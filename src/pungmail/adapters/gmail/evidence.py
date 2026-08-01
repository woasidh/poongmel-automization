from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import requests

from pungmail.adapters.gmail.client import GmailReadOnlyClient
from pungmail.adapters.gmail.parser import ParsedMessage, parse_raw_message
from pungmail.adapters.legacy.extractor import LegacyEvidenceExtractor
from pungmail.config import Settings, get_settings


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class StoredAttachment:
    gmail_message_id: str
    gmail_attachment_id: str
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_path: str
    extraction_status: str
    extracted_text_path: str | None
    ocr_text_path: str | None
    warnings: tuple[str, ...]


class EvidenceStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.evidence_path
        self.root.mkdir(parents=True, exist_ok=True)

    def message_dir(self, thread_id: str, message_id: str) -> Path:
        path = self.root / safe_segment(thread_id) / safe_segment(message_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_bytes(self, path: Path, data: bytes) -> tuple[str, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return self.relative(path), sha256(data).hexdigest()

    def write_text(self, path: Path, value: str) -> tuple[str, str]:
        return self.write_bytes(path, value.encode("utf-8"))

    def write_json(self, path: Path, value: Any) -> tuple[str, str]:
        data = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            default=_json_default,
        ).encode("utf-8")
        return self.write_bytes(path, data)

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.settings.project_root.resolve()).as_posix()

    def resolve(self, stored_path: str | None) -> Path | None:
        if not stored_path:
            return None
        candidate = (self.settings.project_root / stored_path).resolve()
        if self.settings.project_root.resolve() not in candidate.parents:
            raise ValueError("Evidence path escaped project root")
        return candidate


class MessageEvidenceCollector:
    def __init__(
        self,
        gmail: GmailReadOnlyClient,
        settings: Settings | None = None,
        extractor: LegacyEvidenceExtractor | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.gmail = gmail
        self.store = EvidenceStore(self.settings)
        self.extractor = extractor or LegacyEvidenceExtractor(self.settings)

    def store_raw_message(self, raw: dict[str, Any]) -> tuple[ParsedMessage, dict[str, str]]:
        parsed = parse_raw_message(raw)
        message_dir = self.store.message_dir(parsed.thread_id, parsed.message_id)
        raw_path, _ = self.store.write_json(message_dir / "raw-message.json", raw)
        text_path, _ = self.store.write_text(message_dir / "body.txt", parsed.body_text)
        html_path, _ = self.store.write_text(message_dir / "body.html", parsed.body_html)
        actual_path, _ = self.store.write_text(message_dir / "actual-body.txt", parsed.actual_body)
        quoted_path, _ = self.store.write_text(message_dir / "quoted-body.txt", parsed.quoted_body)
        normalized = {
            "message_id": parsed.message_id,
            "thread_id": parsed.thread_id,
            "sender": parsed.sender,
            "recipients": parsed.recipients,
            "cc": parsed.cc,
            "subject": parsed.subject,
            "body": parsed.body_text,
            "attachment_names": [part.filename for part in parsed.attachments],
        }
        _, content_hash = self.store.write_json(message_dir / "normalized.json", normalized)
        return parsed, {
            "raw_message_path": raw_path,
            "body_text_path": text_path,
            "body_html_path": html_path,
            "actual_body_path": actual_path,
            "quoted_body_path": quoted_path,
            "content_sha256": content_hash,
        }

    def extract_attachments(self, raw: dict[str, Any]) -> list[StoredAttachment]:
        parsed = parse_raw_message(raw)
        message_dir = self.store.message_dir(parsed.thread_id, parsed.message_id)
        attachment_dir = message_dir / "attachments"
        results: list[StoredAttachment] = []
        for index, part in enumerate(parsed.attachments, start=1):
            warnings: list[str] = []
            try:
                data = part.inline_data
                if data is None and part.attachment_id:
                    data = self.gmail.download_attachment(parsed.message_id, part.attachment_id)
                if data is None:
                    raise RuntimeError("attachment has no downloadable data")
                if len(data) > self.settings.max_attachment_bytes:
                    raise RuntimeError("attachment exceeds configured size limit")
                file_name = safe_filename(part.filename, f"attachment-{index:02d}")
                raw_path, digest = self.store.write_bytes(attachment_dir / file_name, data)
                extraction = self.extractor.extract(file_name, part.mime_type, data)
                warnings.extend(extraction.warnings)
                text_path = None
                ocr_path = None
                if extraction.text:
                    text_path, _ = self.store.write_text(
                        attachment_dir / f"{file_name}.extracted.txt", extraction.text
                    )
                if extraction.ocr_text:
                    ocr_path, _ = self.store.write_text(
                        attachment_dir / f"{file_name}.ocr.txt", extraction.ocr_text
                    )
                results.append(
                    StoredAttachment(
                        gmail_message_id=parsed.message_id,
                        gmail_attachment_id=part.attachment_id or f"inline-{index}",
                        file_name=file_name,
                        mime_type=part.mime_type,
                        size_bytes=len(data),
                        sha256=digest,
                        storage_path=raw_path,
                        extraction_status=extraction.status,
                        extracted_text_path=text_path,
                        ocr_text_path=ocr_path,
                        warnings=tuple(warnings),
                    )
                )
            except Exception as exc:
                results.append(
                    StoredAttachment(
                        gmail_message_id=parsed.message_id,
                        gmail_attachment_id=part.attachment_id or f"inline-{index}",
                        file_name=safe_filename(part.filename, f"attachment-{index:02d}"),
                        mime_type=part.mime_type,
                        size_bytes=part.size_bytes,
                        sha256="",
                        storage_path="",
                        extraction_status="READ_FAILED",
                        extracted_text_path=None,
                        ocr_text_path=None,
                        warnings=(f"{type(exc).__name__}: {exc}",),
                    )
                )
        results.extend(self._extract_large_attachments(parsed, attachment_dir))
        return results

    def _extract_large_attachments(
        self,
        message: ParsedMessage,
        attachment_dir: Path,
    ) -> list[StoredAttachment]:
        results: list[StoredAttachment] = []
        approved = [link for link in message.links if is_approved_large_attachment_url(link)]
        for index, link in enumerate(approved, start=1):
            synthetic_id = f"large-{sha256(link.encode('utf-8')).hexdigest()[:20]}"
            try:
                response = requests.get(link, timeout=45, allow_redirects=True)
                response.raise_for_status()
                if not is_approved_large_attachment_url(response.url):
                    raise RuntimeError("large attachment redirected outside approved endpoint")
                data = response.content
                if len(data) > self.settings.max_attachment_bytes:
                    raise RuntimeError("large attachment exceeds configured size limit")
                fallback = f"large-attachment-{index:02d}"
                file_name = safe_filename(
                    self.extractor.module._large_attachment_filename(
                        response.headers.get("Content-Disposition", ""), fallback
                    ),
                    fallback,
                )
                mime_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                raw_path, digest = self.store.write_bytes(attachment_dir / file_name, data)
                extraction = self.extractor.extract(file_name, mime_type, data)
                text_path = None
                ocr_path = None
                if extraction.text:
                    text_path, _ = self.store.write_text(
                        attachment_dir / f"{file_name}.extracted.txt", extraction.text
                    )
                if extraction.ocr_text:
                    ocr_path, _ = self.store.write_text(
                        attachment_dir / f"{file_name}.ocr.txt", extraction.ocr_text
                    )
                results.append(
                    StoredAttachment(
                        gmail_message_id=message.message_id,
                        gmail_attachment_id=synthetic_id,
                        file_name=file_name,
                        mime_type=mime_type or "application/octet-stream",
                        size_bytes=len(data),
                        sha256=digest,
                        storage_path=raw_path,
                        extraction_status=extraction.status,
                        extracted_text_path=text_path,
                        ocr_text_path=ocr_path,
                        warnings=extraction.warnings,
                    )
                )
            except Exception as exc:
                results.append(
                    StoredAttachment(
                        gmail_message_id=message.message_id,
                        gmail_attachment_id=synthetic_id,
                        file_name=f"large-attachment-{index:02d}",
                        mime_type="application/octet-stream",
                        size_bytes=0,
                        sha256="",
                        storage_path="",
                        extraction_status="READ_FAILED",
                        extracted_text_path=None,
                        ocr_text_path=None,
                        warnings=(f"{type(exc).__name__}: {exc}",),
                    )
                )
        return results


def safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:120] or "unknown"


def safe_filename(value: str, fallback: str) -> str:
    cleaned = value.replace("\\", "/").split("/")[-1].strip().strip(".")
    cleaned = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", cleaned)
    return cleaned[:220] or fallback


def is_approved_large_attachment_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == "uc.richwood.net"
        and parsed.path == "/mail/mail002A31"
    )
