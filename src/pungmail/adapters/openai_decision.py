from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from openai import OpenAI

from pungmail.config import Settings, get_settings
from pungmail.domain.decisions import MailClassification, MailDecision
from pungmail.prompts import (
    PromptStage,
    PromptTrace,
    build_category_prompt_bundle,
    build_classification_prompt_bundle,
    build_prompt_trace,
)


MODEL_ID = "gpt-5.4-nano"


class ResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class DecisionResult:
    decision: MailDecision
    response_id: str
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    raw_response_path: str
    raw_response_sha256: str
    prompt_trace: PromptTrace


class DecisionStageError(RuntimeError):
    def __init__(
        self,
        stage: str,
        original_error: Exception,
        prompt_trace: PromptTrace,
    ) -> None:
        super().__init__(f"{stage}: {type(original_error).__name__}: {original_error}")
        self.stage = stage
        self.original_error = original_error
        self.prompt_trace = prompt_trace


def _read_secret(path: Path, key_name: str) -> str | None:
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == key_name:
            return value.strip().strip('"').strip("'") or None
    return None


def load_openai_api_key(settings: Settings | None = None) -> str:
    active = settings or get_settings()
    value = os.getenv("PUNGMAIL_OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY")
    value = value or _read_secret(active.openai_secret_env_path, "OPENAI_API_KEY")
    if not value:
        raise RuntimeError("OpenAI API key is not configured")
    return value


def evidence_bundle_sha256(evidence: dict[str, Any]) -> str:
    encoded = json.dumps(
        evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class OpenAIMailDecisionClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        parser: ResponsesParser | None = None,
    ):
        self.settings = settings or get_settings()
        if self.settings.openai_model != MODEL_ID:
            raise ValueError(f"OpenAI model must be fixed to {MODEL_ID}")
        self.parser = parser or OpenAI(
            api_key=load_openai_api_key(self.settings),
            timeout=self.settings.openai_timeout_seconds,
        ).responses

    def decide(
        self,
        evidence: dict[str, Any],
        catalog_candidates: list[dict[str, object]],
    ) -> DecisionResult:
        started = perf_counter()
        classification_bundle = build_classification_prompt_bundle()
        classification_stage = PromptStage(
            name="CATEGORY_CLASSIFICATION",
            bundle=classification_bundle,
        )
        classification_trace = build_prompt_trace(classification_stage)
        try:
            classification_response = self.parser.parse(
                model=MODEL_ID,
                reasoning={"effort": self.settings.openai_reasoning_effort},
                instructions=classification_bundle.content,
                input=json.dumps({"evidence": evidence}, ensure_ascii=False),
                text_format=MailClassification,
                store=False,
            )
            classification_parsed = classification_response.output_parsed
            if classification_parsed is None:
                raise ValueError("OpenAI classification response was empty")
            classification = (
                classification_parsed
                if isinstance(classification_parsed, MailClassification)
                else MailClassification.model_validate(classification_parsed)
            )
        except Exception as exc:
            raise DecisionStageError(
                classification_stage.name,
                exc,
                classification_trace,
            ) from exc

        category_bundle = build_category_prompt_bundle(classification.category)
        category_stage = PromptStage(
            name="CATEGORY_PROCESSING",
            category=classification.category.value,
            bundle=category_bundle,
        )
        prompt_trace = build_prompt_trace(classification_stage, category_stage)
        user_payload = {
            "selected_category": classification.category.value,
            "instruction": "선택된 카테고리를 변경하지 말고 해당 업무값만 구조화하세요.",
            "evidence": evidence,
            "company_catalog_exact_candidates": catalog_candidates,
        }
        try:
            category_response = self.parser.parse(
                model=MODEL_ID,
                reasoning={"effort": self.settings.openai_reasoning_effort},
                instructions=category_bundle.content,
                input=json.dumps(user_payload, ensure_ascii=False),
                text_format=MailDecision,
                store=False,
            )
            category_parsed = category_response.output_parsed
            if category_parsed is None:
                raise ValueError("OpenAI category processing response was empty")
            decision = (
                category_parsed
                if isinstance(category_parsed, MailDecision)
                else MailDecision.model_validate(category_parsed)
            )
            if decision.category != classification.category:
                raise ValueError(
                    "category processing changed the selected category: "
                    f"{classification.category.value} -> {decision.category.value}"
                )
        except Exception as exc:
            raise DecisionStageError(category_stage.name, exc, prompt_trace) from exc

        latency_ms = round((perf_counter() - started) * 1000)
        classification_raw = classification_response.model_dump(
            mode="json", exclude_none=False, warnings=False
        )
        category_raw = category_response.model_dump(
            mode="json", exclude_none=False, warnings=False
        )
        raw_payload = {
            "classification": classification_raw,
            "category_processing": category_raw,
        }
        raw_json = json.dumps(raw_payload, ensure_ascii=False, indent=2, default=str)
        digest = sha256(raw_json.encode("utf-8")).hexdigest()
        response_id = str(category_response.id)
        target = self.settings.ai_response_path / f"{response_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw_json, encoding="utf-8")
        classification_usage = getattr(classification_response, "usage", None)
        category_usage = getattr(category_response, "usage", None)

        def total_usage(name: str) -> int | None:
            values = [
                getattr(classification_usage, name, None),
                getattr(category_usage, name, None),
            ]
            present = [value for value in values if value is not None]
            return sum(present) if present else None

        return DecisionResult(
            decision=decision,
            response_id=response_id,
            latency_ms=latency_ms,
            input_tokens=total_usage("input_tokens"),
            output_tokens=total_usage("output_tokens"),
            raw_response_path=str(target.relative_to(self.settings.project_root)),
            raw_response_sha256=digest,
            prompt_trace=prompt_trace,
        )
