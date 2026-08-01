from __future__ import annotations

from io import BytesIO

import fitz

from pungmail.adapters.legacy.extractor import extract_pdf_tables
from pungmail.domain.decisions import MailDecision
from pungmail.services.decision_pipeline import (
    AI_ATTACHMENT_TEXT_LIMIT,
    _attachment_text,
    apply_direction_guards,
)


def test_pdf_order_table_preserves_item_quantity_and_delivery_date() -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((40, 60), "item | quantity | delivery")
    page.draw_rect(fitz.Rect(40, 80, 500, 150))
    page.draw_line(fitz.Point(40, 110), fitz.Point(500, 110))
    page.draw_line(fitz.Point(230, 80), fitz.Point(230, 150))
    page.draw_line(fitz.Point(340, 80), fitz.Point(340, 150))
    page.insert_text((50, 100), "ITEM")
    page.insert_text((240, 100), "QTY")
    page.insert_text((350, 100), "DELIVERY")
    page.insert_text((50, 135), "BT-12")
    page.insert_text((240, 135), "126 kg")
    page.insert_text((350, 135), "2026-08-10")
    data = document.tobytes()
    document.close()

    table_text = extract_pdf_tables(data)

    assert "BT-12" in table_text
    assert "126 kg" in table_text
    assert "2026-08-10" in table_text


def test_ai_attachment_text_is_limited_without_changing_source(
    isolated_settings,
) -> None:
    source = isolated_settings.evidence_path / "large.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("가" * (AI_ATTACHMENT_TEXT_LIMIT + 100), encoding="utf-8")
    stored_path = source.relative_to(isolated_settings.project_root).as_posix()

    value, truncated = _attachment_text(isolated_settings, stored_path)

    assert len(value) == AI_ATTACHMENT_TEXT_LIMIT
    assert truncated is True
    assert len(source.read_text(encoding="utf-8")) == AI_ATTACHMENT_TEXT_LIMIT + 100


def test_outgoing_richwood_purchase_order_is_forced_to_upstream_order() -> None:
    decision = MailDecision.model_validate(
        {
            "category": "발주",
            "case_action": "CREATE",
            "case_lookup_keys": {
                "original_message_id": "message-1",
                "thread_id": "thread-1",
                "company_key": "seiwa",
                "po_numbers": ["RW-22619"],
                "rw_numbers": [],
                "si_numbers": [],
                "item_component_keys": [],
            },
            "company": "SEIWA SUPPLY CO., LTD.",
            "subject": "[RICHWOOD] PO22619 request",
            "summary": "상류 공급사 주문",
            "missing_fields": [],
            "evidence_refs": ["gmail:message-1", "attachment:po"],
            "category_payload": {
                "payload_type": "ORDER",
                "po_numbers": ["RW-22619"],
                "items": [],
                "requested_delivery_date": None,
                "notes": [],
            },
        }
    )
    evidence = {
        "messages": [
            {
                "sender_email": "staff@richwood.net",
                "recipients": ["supplier@example.jp"],
                "cc": ["cosmetics@richwood.net"],
                "subject": "[RICHWOOD] PO22619 request",
                "actual_body": "Please see the attached PO22619 sheet.",
            }
        ],
        "attachments": [
            {
                "extracted_text": (
                    "PURCHASE ORDER SHEET RW-22619\n"
                    "TO : SEIWA SUPPLY CO., LTD. FROM : RICHWOOD TRADING CO., LTD."
                )
            }
        ],
    }

    guarded = apply_direction_guards(decision, evidence)

    assert guarded.category.value == "오더"
    assert guarded.category_payload.payload_type == "UPSTREAM_ORDER"
    assert guarded.category_payload.rw_numbers == ["RW-22619"]
    assert guarded.case_lookup_keys.po_numbers == []
