from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pungmail.adapters.catalog import CompanyCatalog
from pungmail.adapters.openai_decision import MODEL_ID, OpenAIMailDecisionClient
from pungmail.config import Settings
from pungmail.domain.decisions import MailClassification, MailDecision
from pungmail.prompts import (
    PromptStage,
    build_category_prompt_bundle,
    build_classification_prompt_bundle,
    build_prompt_bundle,
    build_prompt_trace,
)


def sample_decision() -> dict[str, object]:
    return {
        "category": "샘플자료견적",
        "case_action": "CREATE",
        "case_lookup_keys": {
            "original_message_id": "message-1",
            "thread_id": "thread-1",
            "company_key": "customer-a",
            "po_numbers": [],
            "rw_numbers": [],
            "si_numbers": [],
            "item_component_keys": ["VC-IP|SAMPLE"],
        },
        "company": "고객사 A",
        "subject": "VC-IP 샘플 요청",
        "summary": "VC-IP 샘플을 요청함",
        "missing_fields": [],
        "evidence_refs": ["gmail:message-1"],
        "category_payload": {
            "payload_type": "SAMPLE_DOCUMENT_QUOTE",
            "items": [
                {
                    "raw_product_name": "VC-IP",
                    "item_code": "30500314",
                    "spec": "5KG",
                    "quantity": "1",
                    "unit": "EA",
                }
            ],
            "components": [
                {
                    "component_type": "SAMPLE",
                    "label": "VC-IP 샘플",
                    "completed": False,
                    "evidence_ref": "gmail:message-1",
                }
            ],
            "end_user": None,
            "destination": None,
            "recipient": None,
            "contact": None,
        },
    }


def test_structured_output_rejects_category_payload_mismatch() -> None:
    payload = sample_decision()
    payload["category"] = "발주"
    with pytest.raises(ValueError, match="does not match"):
        MailDecision.model_validate(payload)


def test_openai_schema_uses_supported_union_shape() -> None:
    schema = MailDecision.model_json_schema()
    payload_schema = schema["properties"]["category_payload"]
    assert "oneOf" not in payload_schema
    assert len(payload_schema["anyOf"]) == 7


def test_prompt_manifest_is_deterministic_and_complete() -> None:
    first = build_prompt_bundle()
    second = build_prompt_bundle()
    assert first.sha256 == second.sha256
    assert first.version == "issue2-real-v4"
    assert len(first.files) == 9
    assert all(len(item.sha256) == 64 for item in first.files)
    assert "메일 한 건" in first.content
    assert "최초 주문 방향은 바뀌지 않는다" in first.content
    assert "해외업무보다 풍림자료요청을 우선" in first.content
    assert "고객이 시작한 업무 방향은 바뀌지 않는다" in first.content


def test_prompt_trace_contains_only_prompts_used_by_each_stage() -> None:
    classification = build_classification_prompt_bundle()
    category_processing = build_category_prompt_bundle("해외업무")
    trace = build_prompt_trace(
        PromptStage(name="CATEGORY_CLASSIFICATION", bundle=classification),
        PromptStage(
            name="CATEGORY_PROCESSING",
            category="해외업무",
            bundle=category_processing,
        ),
    )

    assert [item.path for item in classification.files] == [
        "common.md",
        "classification.md",
    ]
    assert [item.path for item in category_processing.files] == [
        "common.md",
        "categories/overseas_work.md",
    ]
    assert trace.manifest()["stages"][1]["category"] == "해외업무"


def test_duplicate_request_components_keep_completed_evidence() -> None:
    payload = sample_decision()
    category_payload = payload["category_payload"]
    assert isinstance(category_payload, dict)
    category_payload["components"] = [
        {
            "component_type": "DOCUMENT",
            "label": "English MSDS",
            "completed": False,
            "evidence_ref": None,
        },
        {
            "component_type": "DOCUMENT",
            "label": "  English   MSDS  ",
            "completed": True,
            "evidence_ref": "attachment:msds",
        },
    ]

    decision = MailDecision.model_validate(payload)
    components = decision.category_payload.components

    assert len(components) == 1
    assert components[0].completed is True
    assert components[0].evidence_ref == "attachment:msds"


def test_company_catalog_retains_duplicates_and_hides_deleted() -> None:
    catalog = CompanyCatalog()
    duplicate = catalog.lookup("D-400")
    assert [(item.item_code, item.spec) for item in duplicate] == [
        ("30500009", "10KG"),
        ("30500010", "50KG"),
    ]
    vc_ip = catalog.lookup("VC-IP")
    assert len(vc_ip) == 1
    assert vc_ip[0].company_display_name == "VC-IP"
    assert vc_ip[0].company_from_product_group == "NIKKO"


class FakeResponse:
    usage = SimpleNamespace(input_tokens=20, output_tokens=10)

    def __init__(self, response_id: str, output_parsed: object) -> None:
        self.id = response_id
        self.output_parsed = output_parsed

    def model_dump(self, **kwargs):  # noqa: ANN003, ANN201
        return {"id": self.id, "output": self.output_parsed.model_dump(mode="json")}


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        if kwargs["text_format"] is MailClassification:
            return FakeResponse(
                "resp_classification",
                MailClassification(category="샘플자료견적"),
            )
        return FakeResponse(
            "resp_category_processing",
            MailDecision.model_validate(sample_decision()),
        )


def test_openai_adapter_uses_two_stage_fixed_model_calls(tmp_path: Path) -> None:
    parser = FakeResponses()
    settings = Settings(
        project_root=tmp_path,
        database_path=tmp_path / "test.db",
        evidence_path=tmp_path / "evidence",
        ai_response_path=tmp_path / "ai-responses",
        log_path=tmp_path / "test.log",
        prefect_home=tmp_path / "prefect",
    )
    client = OpenAIMailDecisionClient(settings, parser=parser)
    result = client.decide({"message_id": "message-1"}, [])
    assert len(parser.calls) == 2
    assert all(call["model"] == MODEL_ID for call in parser.calls)
    assert parser.calls[0]["text_format"] is MailClassification
    assert "# 카테고리 분류" in parser.calls[0]["instructions"]
    assert "# 샘플·자료·견적" not in parser.calls[0]["instructions"]
    assert parser.calls[1]["text_format"] is MailDecision
    assert "# 카테고리 분류" not in parser.calls[1]["instructions"]
    assert "# 샘플·자료·견적" in parser.calls[1]["instructions"]
    assert result.decision.category.value == "샘플자료견적"
    assert [stage.name for stage in result.prompt_trace.stages] == [
        "CATEGORY_CLASSIFICATION",
        "CATEGORY_PROCESSING",
    ]
    assert (tmp_path / result.raw_response_path).exists()
