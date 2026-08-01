from __future__ import annotations

from io import BytesIO

import fitz

from pungmail.adapters.legacy.extractor import extract_pdf_tables
from pungmail.services.decision_pipeline import (
    AI_ATTACHMENT_TEXT_LIMIT,
    _attachment_text,
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
