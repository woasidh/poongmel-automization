from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from typing import Any

from pungmail.config import Settings, get_settings


EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-])[^@\s]*@([\w.-]+)")
SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|access[_-]?token|refresh[_-]?token|password)"
    r"([\"'\s:=]+)([^\s,;\"'}]+)"
)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if re.search(r"(?i)(token|secret|password|api.?key)", key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    masked = SECRET_RE.sub(r"\1\2***", value)
    return EMAIL_RE.sub(lambda match: f"{match.group(1)}***@{match.group(2)}", masked)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in (
            "service",
            "workflow_run_id",
            "prefect_flow_run_id",
            "node_key",
            "gmail_message_id",
            "gmail_thread_id",
            "mail_event_id",
        ):
            value = getattr(record, key, None)
            if value:
                payload[key] = redact(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings | None = None) -> None:
    active = settings or get_settings()
    active.ensure_directories()
    root = logging.getLogger()
    if getattr(root, "_pungmail_configured", False):
        return
    root.setLevel(active.log_level.upper())
    formatter = JsonFormatter()

    file_handler = logging.FileHandler(active.log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)
    setattr(root, "_pungmail_configured", True)


def read_log_lines(
    path: Path,
    *,
    query: str = "",
    level: str = "",
    limit: int = 300,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    query_folded = query.casefold().strip()
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            try:
                row = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if level and row.get("level") != level:
                continue
            if query_folded and query_folded not in json.dumps(row, ensure_ascii=False).casefold():
                continue
            rows.append(row)
    return rows[-limit:][::-1]
