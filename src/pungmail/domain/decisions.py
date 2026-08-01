from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Category(StrEnum):
    ORDER = "발주"
    UPSTREAM_ORDER = "오더"
    SAMPLE_DOCUMENT_QUOTE = "샘플자료견적"
    PUNGLIM_DOCUMENT = "풍림자료요청"
    INTERNAL_WORK = "사내업무"
    OVERSEAS_WORK = "해외업무"
    HOLD = "보류"


class CaseAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"


class CaseLookupKeys(StrictModel):
    original_message_id: str | None
    thread_id: str | None
    company_key: str | None
    po_numbers: list[str]
    rw_numbers: list[str]
    si_numbers: list[str]
    item_component_keys: list[str]


class CatalogItemInput(StrictModel):
    raw_product_name: str
    item_code: str | None
    spec: str | None
    quantity: str | None
    unit: str | None


class RequestComponentInput(StrictModel):
    component_type: Literal["SAMPLE", "DOCUMENT", "QUOTE", "OTHER"]
    label: str
    completed: bool
    evidence_ref: str | None


class OrderPayload(StrictModel):
    payload_type: Literal["ORDER"]
    po_numbers: list[str]
    items: list[CatalogItemInput]
    requested_delivery_date: str | None
    notes: list[str]


class UpstreamOrderPayload(StrictModel):
    payload_type: Literal["UPSTREAM_ORDER"]
    rw_numbers: list[str]
    si_numbers: list[str]
    items: list[CatalogItemInput]
    status_text: str | None
    notes: list[str]


class SampleDocumentQuotePayload(StrictModel):
    payload_type: Literal["SAMPLE_DOCUMENT_QUOTE"]
    items: list[CatalogItemInput]
    components: list[RequestComponentInput]
    end_user: str | None
    destination: str | None
    recipient: str | None
    contact: str | None


class PunglimDocumentRequestPayload(StrictModel):
    payload_type: Literal["PUNGLIM_DOCUMENT"]
    items: list[CatalogItemInput]
    components: list[RequestComponentInput]
    supplier_route: Literal["nikko", "seiwa", "기타"] | None
    recipient: str | None
    contact: str | None


class InternalWorkPayload(StrictModel):
    payload_type: Literal["INTERNAL_WORK"]
    assignee: str | None
    request_detail: str
    due_date: str | None


class OverseasWorkPayload(StrictModel):
    payload_type: Literal["OVERSEAS_WORK"]
    counterparty: str | None
    request_detail: str
    reply_required: bool
    due_date: str | None


class HoldPayload(StrictModel):
    payload_type: Literal["HOLD"]
    failure_node: str | None
    failure_type: str
    unresolved_values: list[str]
    check_items: list[str]
    retryable: bool


CategoryPayload = (
    OrderPayload
    | UpstreamOrderPayload
    | SampleDocumentQuotePayload
    | PunglimDocumentRequestPayload
    | InternalWorkPayload
    | OverseasWorkPayload
    | HoldPayload
)


_PAYLOAD_CATEGORY = {
    "ORDER": Category.ORDER,
    "UPSTREAM_ORDER": Category.UPSTREAM_ORDER,
    "SAMPLE_DOCUMENT_QUOTE": Category.SAMPLE_DOCUMENT_QUOTE,
    "PUNGLIM_DOCUMENT": Category.PUNGLIM_DOCUMENT,
    "INTERNAL_WORK": Category.INTERNAL_WORK,
    "OVERSEAS_WORK": Category.OVERSEAS_WORK,
    "HOLD": Category.HOLD,
}


class MailDecision(StrictModel):
    category: Category
    case_action: CaseAction
    case_lookup_keys: CaseLookupKeys
    company: str | None
    subject: str
    summary: str
    missing_fields: list[str]
    evidence_refs: list[str]
    category_payload: CategoryPayload

    @model_validator(mode="after")
    def category_matches_payload(self) -> MailDecision:
        expected = _PAYLOAD_CATEGORY[self.category_payload.payload_type]
        if self.category != expected:
            raise ValueError(
                f"category {self.category} does not match payload {self.category_payload.payload_type}"
            )
        return self
