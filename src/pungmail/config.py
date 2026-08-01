from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="PUNGMAIL_",
        extra="ignore",
    )

    project_root: Path = PROJECT_ROOT
    database_path: Path = PROJECT_ROOT / "data" / "pungmail.db"
    evidence_path: Path = PROJECT_ROOT / "runtime" / "evidence"
    ai_response_path: Path = PROJECT_ROOT / "runtime" / "ai-responses"
    card_path: Path = PROJECT_ROOT / "runtime" / "cards"
    log_path: Path = PROJECT_ROOT / "runtime" / "logs" / "pungmail.jsonl"
    prefect_home: Path = PROJECT_ROOT / "runtime" / "prefect"
    prefect_api_url: str = "http://127.0.0.1:4200/api"

    legacy_project_path: Path = Path(r"C:\Users\ASUS\Documents\New project")
    gmail_credentials_path: Path = Path(
        r"C:\Users\ASUS\Documents\New project\credentials.json"
    )
    gmail_token_path: Path = Path(r"C:\Users\ASUS\Documents\New project\token.json")
    gmail_enabled: bool = True

    openai_model: str = "gpt-5.4-nano"
    openai_reasoning_effort: str = "low"
    openai_timeout_seconds: int = Field(default=90, ge=10, le=300)
    openai_secret_env_path: Path = Path(
        r"C:\Users\ASUS\Documents\New project\.final_review_secrets.env"
    )
    company_catalog_path: Path = Path(
        r"C:\Users\ASUS\.codex\skills\distinguish-beauty-item-names\references\company-beauty-items.ndjson"
    )
    discord_mode: str = "PREVIEW"
    discord_test_webhooks_json: str = "{}"
    outbox_max_attempts: int = Field(default=5, ge=1, le=20)

    mail_check_interval_seconds: int = Field(default=60, ge=10, le=3600)
    max_messages_per_run: int = Field(default=25, ge=1, le=200)
    history_page_size: int = Field(default=100, ge=1, le=500)
    history_recovery_days: int = Field(default=7, ge=1, le=90)
    max_attachment_bytes: int = Field(default=25 * 1024 * 1024, ge=1024)
    max_archive_entries: int = Field(default=100, ge=1, le=1000)
    max_archive_entry_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_archive_total_bytes: int = Field(default=80 * 1024 * 1024, ge=1024)
    tesseract_cmd: Path = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    ocr_languages: str = "kor+eng"
    log_level: str = "INFO"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.resolve().as_posix()}"

    def ensure_directories(self) -> None:
        for path in (
            self.database_path.parent,
            self.evidence_path,
            self.ai_response_path,
            self.card_path,
            self.log_path.parent,
            self.prefect_home,
            self.project_root / "runtime" / "backups",
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
