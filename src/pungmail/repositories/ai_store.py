from __future__ import annotations

import json

from pungmail.adapters.openai_decision import DecisionResult, MODEL_ID, evidence_bundle_sha256
from pungmail.domain.decisions import MailDecision
from pungmail.prompts import PromptTrace
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import AIDecision


def save_ai_success(
    mail_event_id: str,
    result: DecisionResult,
    evidence: dict[str, object],
    *,
    reasoning_effort: str,
) -> str:
    with session_scope() as session:
        row = AIDecision(
            mail_event_id=mail_event_id,
            model_id=MODEL_ID,
            response_id=result.response_id,
            reasoning_effort=reasoning_effort,
            prompt_version=result.prompt_trace.version,
            prompt_bundle_sha256=result.prompt_trace.sha256,
            prompt_manifest_json=json.dumps(
                result.prompt_trace.manifest(), ensure_ascii=False
            ),
            evidence_bundle_sha256=evidence_bundle_sha256(evidence),
            raw_response_path=result.raw_response_path,
            raw_response_sha256=result.raw_response_sha256,
            parsed_payload_json=result.decision.model_dump_json(),
            category=result.decision.category.value,
            case_action=result.decision.case_action.value,
            status="SUCCEEDED",
            latency_ms=result.latency_ms,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
        session.add(row)
        session.flush()
        return row.id


def save_ai_failure(
    mail_event_id: str,
    error: Exception,
    evidence: dict[str, object],
    prompt_trace: PromptTrace,
    hold_decision: MailDecision,
    *,
    reasoning_effort: str,
) -> str:
    with session_scope() as session:
        row = AIDecision(
            mail_event_id=mail_event_id,
            model_id=MODEL_ID,
            reasoning_effort=reasoning_effort,
            prompt_version=prompt_trace.version,
            prompt_bundle_sha256=prompt_trace.sha256,
            prompt_manifest_json=json.dumps(prompt_trace.manifest(), ensure_ascii=False),
            evidence_bundle_sha256=evidence_bundle_sha256(evidence),
            parsed_payload_json=hold_decision.model_dump_json(),
            category=hold_decision.category.value,
            case_action=hold_decision.case_action.value,
            status="FAILED_TO_HOLD",
            error_type=type(error).__name__,
            error_message=str(error)[:4000],
        )
        session.add(row)
        session.flush()
        return row.id
