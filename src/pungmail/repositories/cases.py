from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re
from uuid import uuid4

from sqlalchemy import delete, select

from pungmail.adapters.catalog import CompanyCatalog
from pungmail.domain.decisions import (
    CaseLookupKeys,
    Category,
    HoldPayload,
    MailDecision,
    PunglimDocumentRequestPayload,
    SampleDocumentQuotePayload,
)
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import (
    BusinessCase,
    CaseHistory,
    CaseLookupKey,
    MailEvent,
    RequestCase,
    RequestComponent,
    RequestItem,
)


@dataclass(frozen=True)
class AppliedCase:
    business_case_id: str
    category: Category
    event_action: str
    revision: int
    status: str
    ambiguous_case_ids: tuple[str, ...] = ()


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def lookup_key_groups(keys: CaseLookupKeys) -> list[list[tuple[str, str]]]:
    groups: list[list[tuple[str, str]]] = []
    company = _key(keys.company_key or "")
    if keys.original_message_id:
        groups.append([("GMAIL_MESSAGE", _key(keys.original_message_id))])
    if keys.thread_id and company:
        groups.append([("THREAD_COMPANY", f"{_key(keys.thread_id)}|{company}")])
    po = [("COMPANY_PO", f"{company}|{_key(value)}") for value in keys.po_numbers if company]
    if po:
        groups.append(po)
    rw = [("COMPANY_RW", f"{company}|{_key(value)}") for value in keys.rw_numbers if company]
    if rw:
        groups.append(rw)
    si = [("SI", _key(value)) for value in keys.si_numbers]
    if si:
        groups.append(si)
    item_components = [
        ("COMPANY_ITEM_COMPONENT", f"{company}|{_key(value)}")
        for value in keys.item_component_keys
        if company
    ]
    if item_components:
        groups.append(item_components)
    return groups


def all_lookup_keys(keys: CaseLookupKeys) -> list[tuple[str, str]]:
    return [item for group in lookup_key_groups(keys) for item in group]


def _find_candidates(session, keys: CaseLookupKeys) -> tuple[str | None, tuple[str, ...]]:  # noqa: ANN001
    for group in lookup_key_groups(keys):
        found: set[str] = set()
        for key_type, key_value in group:
            rows = session.scalars(
                select(CaseLookupKey).where(
                    CaseLookupKey.key_type == key_type,
                    CaseLookupKey.key_value == key_value,
                )
            ).all()
            found.update(row.business_case_id for row in rows)
        if len(found) == 1:
            return next(iter(found)), ()
        if len(found) > 1:
            return None, tuple(sorted(found))
    return None, ()


def _hold_for_ambiguity(
    decision: MailDecision, candidate_ids: tuple[str, ...]
) -> MailDecision:
    return decision.model_copy(
        update={
            "category": Category.HOLD,
            "summary": "기존 업무 후보가 여러 개여서 자동 연결하지 않았습니다.",
            "missing_fields": [*decision.missing_fields, "기존 업무 단일 식별자"],
            "category_payload": HoldPayload(
                payload_type="HOLD",
                failure_node="resolve_case",
                failure_type="AMBIGUOUS_CASE_MATCH",
                unresolved_values=list(candidate_ids),
                check_items=["Gmail/PO/RW/SI 또는 품목·요청 구성 식별자를 확인하세요."],
                retryable=False,
            ),
        }
    )


def _case_status(decision: MailDecision) -> str:
    if decision.category == Category.HOLD:
        return "HOLD"
    payload = decision.category_payload
    if isinstance(payload, (SampleDocumentQuotePayload, PunglimDocumentRequestPayload)):
        return "COMPLETED" if payload.components and all(c.completed for c in payload.components) else "IN_PROGRESS"
    if decision.category in (Category.ORDER, Category.UPSTREAM_ORDER):
        return "PREVIEW"
    return "IN_PROGRESS"


def _case_snapshot(case: BusinessCase | None) -> dict[str, object]:
    if case is None:
        return {}
    return {
        "category": case.category,
        "status": case.status,
        "company": case.company,
        "subject": case.subject,
        "summary": case.summary,
        "payload": json.loads(case.payload_json),
        "revision": case.current_revision,
    }


def _replace_request_data(
    session,  # noqa: ANN001
    case: BusinessCase,
    decision: MailDecision,
    catalog: CompanyCatalog,
) -> None:
    payload = decision.category_payload
    if not isinstance(payload, (SampleDocumentQuotePayload, PunglimDocumentRequestPayload)):
        existing = session.scalar(
            select(RequestCase).where(RequestCase.business_case_id == case.id)
        )
        if existing:
            session.delete(existing)
        return

    request_case = session.scalar(
        select(RequestCase).where(RequestCase.business_case_id == case.id)
    )
    if request_case is None:
        request_case = RequestCase(
            business_case_id=case.id,
            request_type=payload.payload_type,
        )
        session.add(request_case)
        session.flush()
    session.execute(delete(RequestItem).where(RequestItem.request_case_id == request_case.id))
    session.execute(
        delete(RequestComponent).where(RequestComponent.request_case_id == request_case.id)
    )

    resolved_suppliers: set[str] = set()
    for sequence, item in enumerate(payload.items, start=1):
        candidates = catalog.lookup(item.item_code or item.raw_product_name)
        chosen = candidates[0] if len(candidates) == 1 else None
        if chosen:
            resolved_suppliers.add(chosen.company_from_product_group)
        session.add(
            RequestItem(
                request_case_id=request_case.id,
                sequence=sequence,
                raw_product_name=item.raw_product_name,
                company_display_name=chosen.company_display_name if chosen else None,
                item_code=chosen.item_code if chosen else item.item_code,
                spec=chosen.spec if chosen else item.spec,
                company_from_product_group=(
                    chosen.company_from_product_group if chosen else None
                ),
                quantity=item.quantity,
                unit=item.unit,
                catalog_candidates_json=json.dumps(
                    [candidate.as_dict() for candidate in candidates], ensure_ascii=False
                ),
            )
        )
    for sequence, component in enumerate(payload.components, start=1):
        session.add(
            RequestComponent(
                request_case_id=request_case.id,
                component_type=component.component_type,
                label=component.label,
                status="COMPLETED" if component.completed else "PENDING",
                completed_at_utc=datetime.now(UTC) if component.completed else None,
                evidence_ref=component.evidence_ref,
                sequence=sequence,
            )
        )
    request_case.overall_status = case.status
    request_case.recipient = payload.recipient
    request_case.contact = payload.contact
    if isinstance(payload, SampleDocumentQuotePayload):
        request_case.end_user = payload.end_user
        request_case.destination = payload.destination
        request_case.supplier_route = None
    else:
        if len(resolved_suppliers) == 1:
            supplier = next(iter(resolved_suppliers))
            request_case.supplier_route = (
                "nikko" if supplier == "NIKKO" else "seiwa" if supplier == "SEIWA" else "기타"
            )
        else:
            request_case.supplier_route = "기타"


def apply_decision(
    mail_event_id: str,
    decision: MailDecision,
    *,
    ai_decision_id: str | None = None,
    catalog: CompanyCatalog | None = None,
) -> AppliedCase:
    active_catalog = catalog or CompanyCatalog()
    with session_scope() as session:
        event = session.get(MailEvent, mail_event_id)
        if event is None:
            raise LookupError(f"mail event not found: {mail_event_id}")
        if event.business_case_id:
            existing = session.get(BusinessCase, event.business_case_id)
            if existing is None:
                raise RuntimeError("mail event points to missing business case")
            return AppliedCase(
                existing.id,
                Category(existing.category),
                event.event_action or "UPDATE",
                event.applied_revision or existing.current_revision,
                existing.status,
            )

        candidate_id, ambiguous = _find_candidates(session, decision.case_lookup_keys)
        effective = _hold_for_ambiguity(decision, ambiguous) if ambiguous else decision
        if candidate_id:
            case = session.get(BusinessCase, candidate_id)
            if case is None:
                raise RuntimeError("resolved business case disappeared")
            if case.category != effective.category.value:
                ambiguous = (case.id,)
                effective = _hold_for_ambiguity(decision, ambiguous)
                case = None
            action = "UPDATE" if case else "HOLD"
        else:
            case = None
            action = "HOLD" if ambiguous else "CREATE"

        before = _case_snapshot(case)
        if case is None:
            case = BusinessCase(
                category=effective.category.value,
                case_key=f"CASE-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:8].upper()}",
                status=_case_status(effective),
                company=effective.company,
                subject=effective.subject,
                summary=effective.summary,
                payload_json=effective.category_payload.model_dump_json(),
                current_revision=1,
            )
            session.add(case)
            session.flush()
            revision = 1
        else:
            revision = case.current_revision + 1
            case.category = effective.category.value
            case.status = _case_status(effective)
            case.company = effective.company
            case.subject = effective.subject
            case.summary = effective.summary
            case.payload_json = effective.category_payload.model_dump_json()
            case.current_revision = revision
            case.updated_at_utc = datetime.now(UTC)

        for key_type, key_value in all_lookup_keys(effective.case_lookup_keys):
            existing_key = session.scalar(
                select(CaseLookupKey).where(
                    CaseLookupKey.business_case_id == case.id,
                    CaseLookupKey.key_type == key_type,
                    CaseLookupKey.key_value == key_value,
                )
            )
            if existing_key is None:
                session.add(
                    CaseLookupKey(
                        business_case_id=case.id,
                        key_type=key_type,
                        key_value=key_value,
                    )
                )

        _replace_request_data(session, case, effective, active_catalog)
        after = _case_snapshot(case)
        after["revision"] = revision
        session.add(
            CaseHistory(
                business_case_id=case.id,
                revision=revision,
                source_mail_event_id=event.id,
                before_json=json.dumps(before, ensure_ascii=False),
                after_json=json.dumps(after, ensure_ascii=False),
                change_summary=(
                    "기존 업무 갱신" if action == "UPDATE" else "보류 업무 생성" if action == "HOLD" else "신규 업무 생성"
                ),
            )
        )
        event.business_case_id = case.id
        event.ai_decision_id = ai_decision_id
        event.event_action = action
        event.applied_revision = revision
        event.status = "CASE_APPLIED"
        event.current_node = "resolve_case"
        return AppliedCase(
            case.id,
            effective.category,
            action,
            revision,
            case.status,
            ambiguous,
        )
