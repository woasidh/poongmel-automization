from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from pungmail.adapters.catalog import CompanyCatalog
from pungmail.adapters.openai_decision import MODEL_ID, OpenAIMailDecisionClient
from pungmail.config import Settings
from pungmail.domain.decisions import MailDecision
from pungmail.prompts import build_prompt_bundle


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
    assert first.version == "issue2-v1"
    assert len(first.files) == 9
    assert all(len(item.sha256) == 64 for item in first.files)
    assert "메일 한 건" in first.content


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
    id = "resp_test"
    usage = SimpleNamespace(input_tokens=20, output_tokens=10)

    def __init__(self) -> None:
        self.output_parsed = MailDecision.model_validate(sample_decision())

    def model_dump(self, **kwargs):  # noqa: ANN003, ANN201
        return {"id": self.id, "output": self.output_parsed.model_dump(mode="json")}


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        return FakeResponse()


def test_openai_adapter_uses_one_fixed_model_call(tmp_path: Path) -> None:
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
    assert len(parser.calls) == 1
    assert parser.calls[0]["model"] == MODEL_ID
    assert parser.calls[0]["text_format"] is MailDecision
    assert result.decision.category.value == "샘플자료견적"
    assert (tmp_path / result.raw_response_path).exists()
