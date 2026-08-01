from __future__ import annotations

from base64 import urlsafe_b64decode
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses
import html
import re
from typing import Any


HARD_EXCLUDED_LABELS = {"SPAM", "TRASH", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}
QUOTE_MARKERS = (
    "-----Original Message-----",
    "----- 원본 메시지 -----",
    "보낸 사람:",
    "From:",
)


@dataclass(frozen=True)
class AttachmentPart:
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    inline_data: bytes | None = None


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    thread_id: str
    history_id: str
    internal_date_utc: datetime | None
    sender: str
    sender_email: str
    recipients: tuple[str, ...]
    cc: tuple[str, ...]
    subject: str
    labels: tuple[str, ...]
    body_text: str
    body_html: str
    actual_body: str
    quoted_body: str
    links: tuple[str, ...]
    attachments: tuple[AttachmentPart, ...]


def parse_raw_message(raw: dict[str, Any]) -> ParsedMessage:
    payload = raw.get("payload", {}) or {}
    headers = {
        str(header.get("name") or "").casefold(): str(header.get("value") or "")
        for header in payload.get("headers", []) or []
    }
    sender = headers.get("from", "")
    sender_pairs = getaddresses([sender])
    sender_email = sender_pairs[0][1].casefold() if sender_pairs else ""
    recipients = tuple(
        address.casefold()
        for _name, address in getaddresses([headers.get("to", "")])
        if address
    )
    cc = tuple(
        address.casefold()
        for _name, address in getaddresses([headers.get("cc", "")])
        if address
    )
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachment_parts: list[AttachmentPart] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "")
        filename = str(part.get("filename") or "").strip()
        body = part.get("body", {}) or {}
        body_data = str(body.get("data") or "")
        headers_by_name = {
            str(item.get("name") or "").casefold(): str(item.get("value") or "")
            for item in part.get("headers", []) or []
        }
        inline = "inline" in headers_by_name.get("content-disposition", "").casefold()
        if filename and not inline:
            attachment_parts.append(
                AttachmentPart(
                    attachment_id=str(body.get("attachmentId") or ""),
                    filename=filename,
                    mime_type=mime_type or "application/octet-stream",
                    size_bytes=int(body.get("size") or 0),
                    inline_data=_decode(body_data) if body_data else None,
                )
            )
        elif body_data and mime_type == "text/plain":
            text_parts.append(_decode(body_data).decode("utf-8", errors="replace"))
        elif body_data and mime_type == "text/html":
            html_parts.append(_decode(body_data).decode("utf-8", errors="replace"))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    body_text = _normalize("\n\n".join(text_parts))
    body_html = "\n\n".join(html_parts).strip()
    if not body_text and body_html:
        body_text = html_to_text(body_html)
    actual_body, quoted_body = split_actual_and_quoted(body_text)
    internal_date = None
    if str(raw.get("internalDate") or "").isdigit():
        internal_date = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=UTC)
    return ParsedMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=str(raw.get("threadId") or ""),
        history_id=str(raw.get("historyId") or ""),
        internal_date_utc=internal_date,
        sender=sender,
        sender_email=sender_email,
        recipients=recipients,
        cc=cc,
        subject=headers.get("subject", "(제목 없음)"),
        labels=tuple(str(value) for value in raw.get("labelIds", []) or []),
        body_text=body_text,
        body_html=body_html,
        actual_body=actual_body,
        quoted_body=quoted_body,
        links=tuple(extract_links(body_html)),
        attachments=tuple(attachment_parts),
    )


def hard_exclusion_reason(message: ParsedMessage) -> str | None:
    labels = set(message.labels)
    excluded = sorted(labels.intersection(HARD_EXCLUDED_LABELS))
    if excluded:
        return f"excluded_label:{','.join(excluded)}"
    if labels and "INBOX" not in labels:
        return "not_inbox"
    addresses = (message.sender_email, *message.recipients, *message.cc)
    if not any("richwood" in address.casefold() for address in addresses):
        return "not_richwood_related"
    return None


def split_actual_and_quoted(body: str) -> tuple[str, str]:
    if not body:
        return "", ""
    indexes = [body.find(marker) for marker in QUOTE_MARKERS if body.find(marker) >= 0]
    on_wrote = re.search(r"(?im)^On .+ wrote:\s*$", body)
    if on_wrote:
        indexes.append(on_wrote.start())
    quote_lines = re.search(r"(?m)^>+\s?", body)
    if quote_lines:
        indexes.append(quote_lines.start())
    if not indexes:
        return body.strip(), ""
    split_at = min(indexes)
    return body[:split_at].strip(), body[split_at:].strip()


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>", "\n", value)
    value = re.sub(r"(?i)</p>", "\n", value)
    value = re.sub(r"(?s)<.*?>", " ", value)
    return _normalize(html.unescape(value))


def extract_links(value: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    patterns = (
        r'''<a\b[^>]*href=["']([^"']+)["']''',
        r"https?://[^\s\"'<>)]+",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, value or "", re.IGNORECASE):
            link = html.unescape(match.group(1) if match.lastindex else match.group(0)).strip()
            if link and link not in seen:
                seen.add(link)
                links.append(link)
    return links


def _decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _normalize(value: str) -> str:
    value = re.sub(r"[ \t]+", " ", value)
    return re.sub(r"\n{3,}", "\n\n", value).strip()
