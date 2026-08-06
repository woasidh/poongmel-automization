from __future__ import annotations

from datetime import timedelta
from typing import Any

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import (
    FlowRunFilter,
    FlowRunFilterDeploymentId,
    FlowRunFilterState,
    FlowRunFilterStateType,
)
from prefect.client.schemas.objects import StateType
from prefect.client.schemas.schedules import IntervalSchedule
from prefect.states import Cancelled

from pungmail.config import Settings, get_settings


MAIL_DEPLOYMENT_NAME = "mail_processing/local-mail-processing"


def sync_mail_schedule(settings: Settings | None = None) -> dict[str, Any]:
    active = settings or get_settings()
    interval = IntervalSchedule(
        interval=timedelta(seconds=active.mail_check_interval_seconds)
    )
    cancelled_runs = 0

    with get_client(sync_client=True) as client:
        deployment = client.read_deployment_by_name(MAIL_DEPLOYMENT_NAME)
        schedules = client.read_deployment_schedules(deployment.id)
        if schedules:
            primary = schedules[0]
            client.update_deployment_schedule(
                deployment.id,
                primary.id,
                active=active.mail_schedule_enabled,
                schedule=interval,
            )
            for duplicate in schedules[1:]:
                client.delete_deployment_schedule(deployment.id, duplicate.id)
        else:
            client.create_deployment_schedules(
                deployment.id,
                [(interval, active.mail_schedule_enabled)],
            )

        if not active.mail_schedule_enabled:
            active_run_filter = FlowRunFilter(
                deployment_id=FlowRunFilterDeploymentId(any_=[deployment.id]),
                state=FlowRunFilterState(
                    type=FlowRunFilterStateType(
                        any_=[
                            StateType.SCHEDULED,
                            StateType.PENDING,
                            StateType.RUNNING,
                        ]
                    )
                ),
            )
            while True:
                runs = client.read_flow_runs(
                    flow_run_filter=active_run_filter,
                    limit=200,
                )
                if not runs:
                    break
                for run in runs:
                    client.set_flow_run_state(
                        run.id,
                        Cancelled(
                            message=(
                                "Automatic Gmail polling disabled during stabilization"
                            )
                        ),
                        force=True,
                    )
                cancelled_runs += len(runs)

    return {
        "enabled": active.mail_schedule_enabled,
        "interval_seconds": active.mail_check_interval_seconds,
        "cancelled_runs": cancelled_runs,
    }
