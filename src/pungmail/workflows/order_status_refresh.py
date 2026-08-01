from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from prefect import flow, task
from prefect.context import get_run_context

from pungmail.observability.logging import configure_logging
from pungmail.repositories.tracking import (
    create_workflow_run,
    finish_workflow_run,
    record_skipped_node,
)
from pungmail.workflows.graph import ORDER_REFRESH_GRAPH


def _flow_run_id() -> str | None:
    try:
        return str(get_run_context().flow_run.id)
    except Exception:
        return None


@task(name="시간별 갱신 골격 확인")
def record_issue_one_skeleton(workflow_run_id: str) -> dict[str, Any]:
    for node in ORDER_REFRESH_GRAPH:
        record_skipped_node(
            workflow_run_id,
            node.key,
            reason="이슈 4에서 구현하는 시간별 발주 갱신 노드",
        )
    return {"skeleton": True, "node_count": len(ORDER_REFRESH_GRAPH)}


@flow(name="order_status_refresh", log_prints=True)
def order_status_refresh(trigger_type: str = "SCHEDULED") -> dict[str, Any]:
    configure_logging()
    run_id = create_workflow_run(
        "order_status_refresh",
        trigger_type=trigger_type,
        prefect_flow_run_id=_flow_run_id(),
    )
    try:
        result = record_issue_one_skeleton(run_id)
        summary = {
            **result,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        finish_workflow_run(run_id, "SUCCEEDED", summary)
        return summary
    except Exception as exc:
        finish_workflow_run(
            run_id,
            "FAILED",
            {"fatal_error": f"{type(exc).__name__}: {exc}"},
        )
        raise
