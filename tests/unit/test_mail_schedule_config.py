from __future__ import annotations

from pathlib import Path

import yaml
from prefect.cli.deploy._models import PrefectYamlModel

from pungmail.config import Settings


def test_mail_schedule_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.mail_schedule_enabled is False
    assert settings.mail_check_interval_seconds == 60


def test_prefect_mail_schedule_has_safe_disabled_default() -> None:
    prefect_path = Path(__file__).resolve().parents[2] / "prefect.yaml"
    config = yaml.safe_load(prefect_path.read_text(encoding="utf-8"))
    PrefectYamlModel.model_validate(config)
    mail_deployment = config["deployments"][0]
    schedule = mail_deployment["schedules"][0]

    assert schedule["active"] is False
    assert schedule["interval"] == 60
