from __future__ import annotations

from pathlib import Path

import pytest

from pungmail.config import Settings, get_settings
from pungmail.repositories.database import get_engine, reset_database_state
from pungmail.repositories.models import Base


@pytest.fixture
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    project_root = tmp_path / "workspace"
    monkeypatch.setenv("PUNGMAIL_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("PUNGMAIL_DATABASE_PATH", str(project_root / "data" / "test.db"))
    monkeypatch.setenv("PUNGMAIL_EVIDENCE_PATH", str(project_root / "runtime" / "evidence"))
    monkeypatch.setenv(
        "PUNGMAIL_AI_RESPONSE_PATH", str(project_root / "runtime" / "ai-responses")
    )
    monkeypatch.setenv("PUNGMAIL_CARD_PATH", str(project_root / "runtime" / "cards"))
    monkeypatch.setenv(
        "PUNGMAIL_LOG_PATH", str(project_root / "runtime" / "logs" / "test.jsonl")
    )
    monkeypatch.setenv("PUNGMAIL_PREFECT_HOME", str(project_root / "runtime" / "prefect"))
    monkeypatch.setenv("PUNGMAIL_GMAIL_ENABLED", "false")
    get_settings.cache_clear()
    reset_database_state()

    settings = get_settings()
    Base.metadata.create_all(get_engine())
    yield settings

    reset_database_state()
    get_settings.cache_clear()
