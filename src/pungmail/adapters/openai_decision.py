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
from pungmail.domain.decisions import MailDecision
from pungmail.prompts import PromptBundle, build_prompt_bundle


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
    prompt_bundle: PromptBundle


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
        bundle = build_prompt_bundle()
        user_payload = {
            "evidence": evidence,
            "company_catalog_exact_candidates": catalog_candidates,
        }
        started = perf_counter()
        response = self.parser.parse(
            model=MODEL_ID,
            reasoning={"effort": self.settings.openai_reasoning_effort},
            instructions=bundle.content,
            input=json.dumps(user_payload, ensure_ascii=False),
            text_format=MailDecision,
            store=False,
        )
        latency_ms = round((perf_counter() - started) * 1000)
        parsed = response.output_parsed
        if parsed is None:
            raise ValueError("OpenAI response did not contain structured output")
        decision = (
            parsed if isinstance(parsed, MailDecision) else MailDecision.model_validate(parsed)
        )
        raw_payload = response.model_dump(
            mode="json", exclude_none=False, warnings=False
        )
        raw_json = json.dumps(raw_payload, ensure_ascii=False, indent=2, default=str)
        digest = sha256(raw_json.encode("utf-8")).hexdigest()
        response_id = str(response.id)
        target = self.settings.ai_response_path / f"{response_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(raw_json, encoding="utf-8")
        usage = getattr(response, "usage", None)
        return DecisionResult(
            decision=decision,
            response_id=response_id,
            latency_ms=latency_ms,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            raw_response_path=str(target.relative_to(self.settings.project_root)),
            raw_response_sha256=digest,
            prompt_bundle=bundle,
        )
