from __future__ import annotations

from base64 import urlsafe_b64encode

from pungmail.adapters.gmail.parser import hard_exclusion_reason, parse_raw_message


def encoded(value: str) -> str:
    return urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def raw_message(*, labels: list[str] | None = None, sender: str = "buyer@richwood.net") -> dict:
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "historyId": "9001",
        "internalDate": "1704067200000",
        "labelIds": labels or ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": f"담당자 <{sender}>"},
                {"name": "To", "value": "sales@example.com"},
                {"name": "Cc", "value": "team@example.com"},
                {"name": "Subject", "value": "발주 요청"},
            ],
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": encoded("신규 요청입니다.\n\nFrom: 이전 담당자\n이전 내용")},
                },
                {
                    "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "filename": "order.xlsx",
                    "body": {"data": encoded("sample-binary"), "size": 13},
                },
            ],
        },
    }


def test_parser_separates_actual_and_quoted_body_and_attachment() -> None:
    parsed = parse_raw_message(raw_message())

    assert parsed.message_id == "message-1"
    assert parsed.sender_email == "buyer@richwood.net"
    assert parsed.actual_body == "신규 요청입니다."
    assert parsed.quoted_body.startswith("From:")
    assert parsed.attachments[0].filename == "order.xlsx"
    assert parsed.attachments[0].inline_data == b"sample-binary"
    assert hard_exclusion_reason(parsed) is None


def test_hard_exclusion_is_deterministic() -> None:
    spam = parse_raw_message(raw_message(labels=["INBOX", "SPAM"]))
    unrelated = parse_raw_message(raw_message(sender="outside@example.com"))

    assert hard_exclusion_reason(spam) == "excluded_label:SPAM"
    assert hard_exclusion_reason(unrelated) == "not_richwood_related"
