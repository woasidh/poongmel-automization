from __future__ import annotations

import json
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from pungmail.config import Settings, get_settings


class DiscordTransport(Protocol):
    def send(self, channel_key: str, body: str) -> str: ...
    def fetch(self, channel_key: str, message_id: str) -> str: ...
    def delete(self, channel_key: str, message_id: str) -> None: ...


class DiscordWebhookTransport:
    def __init__(self, settings: Settings | None = None):
        active = settings or get_settings()
        raw = json.loads(active.discord_test_webhooks_json or "{}")
        if not isinstance(raw, dict):
            raise ValueError("Discord test webhook configuration must be an object")
        self.webhooks = {str(key): str(value) for key, value in raw.items() if value}

    def _webhook(self, channel_key: str) -> str:
        url = self.webhooks.get(channel_key)
        if not url:
            raise KeyError(f"No test Discord webhook configured for channel {channel_key}")
        return url.rstrip("/")

    @staticmethod
    def _with_wait(url: str) -> str:
        parsed = urlsplit(url)
        query = f"{parsed.query}&wait=true" if parsed.query else "wait=true"
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))

    def send(self, channel_key: str, body: str) -> str:
        response = requests.post(
            self._with_wait(self._webhook(channel_key)),
            json={"content": body, "allowed_mentions": {"parse": []}},
            timeout=30,
        )
        response.raise_for_status()
        message_id = str(response.json().get("id") or "")
        if not message_id:
            raise RuntimeError("Discord response did not contain a message id")
        return message_id

    def fetch(self, channel_key: str, message_id: str) -> str:
        response = requests.get(
            f"{self._webhook(channel_key)}/messages/{message_id}", timeout=30
        )
        response.raise_for_status()
        return str(response.json().get("content") or "")

    def delete(self, channel_key: str, message_id: str) -> None:
        response = requests.delete(
            f"{self._webhook(channel_key)}/messages/{message_id}", timeout=30
        )
        if response.status_code != 404:
            response.raise_for_status()
