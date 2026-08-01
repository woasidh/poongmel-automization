from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re

from sqlalchemy import select

from pungmail.config import Settings, get_settings
from pungmail.domain.decisions import Category
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    BusinessCase,
    RequestCase,
    RequestComponent,
    RequestItem,
)


MAX_DISCORD_BODY = 1900


@dataclass(frozen=True)
class RenderedCard:
    business_case_id: str
    channel_key: str
    body: str
    body_path: str
    body_sha256: str
    full_detail_path: str | None


def escape_markdown(value: object) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+\-.!|~>])", r"\\\1", str(value))


def _channel(case: BusinessCase, request: RequestCase | None) -> str:
    category = Category(case.category)
    if category in (Category.ORDER, Category.UPSTREAM_ORDER):
        return "preview_only"
    if category == Category.HOLD:
        return "hold"
    if category == Category.INTERNAL_WORK:
        return "internal_work"
    if category == Category.OVERSEAS_WORK:
        return "overseas_work"
    if case.status == "COMPLETED":
        return "completed"
    if category == Category.SAMPLE_DOCUMENT_QUOTE:
        return "sample_progress"
    route = request.supplier_route if request else "기타"
    return {
        "nikko": "punglim_nikko",
        "seiwa": "punglim_seiwa",
        "기타": "punglim_other",
    }.get(route or "기타", "punglim_other")


def render_case(
    business_case_id: str,
    *,
    settings: Settings | None = None,
) -> RenderedCard:
    active = settings or get_settings()
    with session_scope() as session:
        case = session.get(BusinessCase, business_case_id)
        if case is None:
            raise LookupError(f"business case not found: {business_case_id}")
        request = session.scalar(
            select(RequestCase).where(RequestCase.business_case_id == case.id)
        )
        items = (
            session.scalars(
                select(RequestItem)
                .where(RequestItem.request_case_id == request.id)
                .order_by(RequestItem.sequence)
            ).all()
            if request
            else []
        )
        components = (
            session.scalars(
                select(RequestComponent)
                .where(RequestComponent.request_case_id == request.id)
                .order_by(RequestComponent.sequence)
            ).all()
            if request
            else []
        )
        channel = _channel(case, request)
        lines = [
            "────────────",
            f"**[{escape_markdown(case.category)}] {escape_markdown(case.subject)}**",
            f"업무: `{escape_markdown(case.case_key)}` · revision {case.current_revision}",
            f"상태: **{escape_markdown(case.status)}**",
        ]
        if case.company:
            lines.append(f"업체: {escape_markdown(case.company)}")
        lines.append(f"요약: {escape_markdown(case.summary)}")
        if items:
            lines.append("\n**품목**")
            for item in items:
                name = item.company_display_name or item.raw_product_name
                detail = " / ".join(
                    filter(None, [item.item_code, item.spec, item.quantity, item.unit])
                )
                supplier = item.company_from_product_group
                suffix = f" ({escape_markdown(detail)})" if detail else ""
                if supplier:
                    suffix += f" · 업체(제품군 기준): {escape_markdown(supplier)}"
                lines.append(f"- {escape_markdown(name)}{suffix}")
        if components:
            lines.append("\n**요청 진행**")
            for component in components:
                label = escape_markdown(component.label)
                rendered = f"~~{label}~~ ✅" if component.status == "COMPLETED" else f"{label} ⏳"
                lines.append(f"- {rendered}")
        if request:
            extras = {
                "End user": request.end_user,
                "수령처": request.destination,
                "담당자": request.recipient,
                "연락처": request.contact,
            }
            for label, value in extras.items():
                if value:
                    lines.append(f"{label}: {escape_markdown(value)}")
        if case.category in (Category.ORDER.value, Category.UPSTREAM_ORDER.value):
            lines.append("\n_2단계 구조화 미리보기이며 실제 원장 확인·반영은 다음 단계에서 수행됩니다._")
        full_body = "\n".join(lines)
        target_dir = active.card_path / case.id / f"revision-{case.current_revision}"
        target_dir.mkdir(parents=True, exist_ok=True)
        full_detail_path: Path | None = None
        body = full_body
        if len(body) > MAX_DISCORD_BODY:
            full_detail_path = target_dir / "full-detail.txt"
            full_detail_path.write_text(full_body, encoding="utf-8")
            body = full_body[: MAX_DISCORD_BODY - 90].rstrip() + "\n\n… 전체 상세는 첨부 파일을 확인하세요."
        body_path = target_dir / "discord-card.txt"
        body_path.write_text(body, encoding="utf-8")
        digest = sha256(body.encode("utf-8")).hexdigest()
        case.card_channel_key = channel
        case.card_body_path = str(body_path.relative_to(active.project_root))
        case.card_body_sha256 = digest
        return RenderedCard(
            case.id,
            channel,
            body,
            case.card_body_path,
            digest,
            str(full_detail_path.relative_to(active.project_root)) if full_detail_path else None,
        )
