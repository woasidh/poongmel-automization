from __future__ import annotations

from base64 import urlsafe_b64decode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from pungmail.config import Settings, get_settings


MONITORED_INBOX_QUERY = (
    "in:inbox {from:richwood to:richwood cc:richwood} "
    "-in:spam -in:trash -category:promotions -category:social"
)


@dataclass(frozen=True)
class GmailMessagePage:
    messages: tuple[dict[str, str], ...]
    next_page_token: str = ""
    history_id: str = ""


class GmailHistoryExpiredError(RuntimeError):
    pass


class GmailReadOnlyClient:
    """기존 Gmail token을 파일 수정 없이 사용하는 조회 전용 클라이언트."""

    def __init__(self, settings: Settings | None = None, service: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self.service = service or build(
            "gmail",
            "v1",
            credentials=self._load_read_only_credentials(self.settings.gmail_token_path),
            cache_discovery=False,
        )

    @staticmethod
    def _load_read_only_credentials(token_path: Path) -> Credentials:
        if not token_path.exists():
            raise FileNotFoundError(f"Gmail token file not found: {token_path}")
        credentials = Credentials.from_authorized_user_file(str(token_path))
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as exc:
                raise RuntimeError("Gmail token refresh failed; legacy token was not modified") from exc
        if not credentials.valid:
            raise RuntimeError("Gmail token is not valid; interactive reauthentication is disabled")
        return credentials

    def current_history_id(self) -> str:
        response = self.service.users().getProfile(userId="me").execute()
        return str(response.get("historyId") or "").strip()

    def list_history_messages(
        self,
        start_history_id: str,
        *,
        page_token: str = "",
        max_results: int = 100,
    ) -> GmailMessagePage:
        request = self.service.users().history().list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded"],
            maxResults=max_results,
            pageToken=page_token or None,
        )
        try:
            response = request.execute()
        except HttpError as exc:
            if getattr(exc.resp, "status", None) in {404, 410}:
                raise GmailHistoryExpiredError("Gmail history cursor expired") from exc
            raise

        seen: set[str] = set()
        messages: list[dict[str, str]] = []
        for history in response.get("history", []) or []:
            for added in history.get("messagesAdded", []) or []:
                message = added.get("message", {}) or {}
                message_id = str(message.get("id") or "").strip()
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                messages.append(
                    {
                        "id": message_id,
                        "threadId": str(message.get("threadId") or "").strip(),
                    }
                )
        return GmailMessagePage(
            messages=tuple(messages),
            next_page_token=str(response.get("nextPageToken") or "").strip(),
            history_id=str(response.get("historyId") or start_history_id).strip(),
        )

    def list_recovery_messages(
        self,
        *,
        page_token: str = "",
        max_results: int = 100,
        newer_than_days: int = 7,
    ) -> GmailMessagePage:
        query = f"newer_than:{newer_than_days}d {MONITORED_INBOX_QUERY}"
        response = (
            self.service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token or None,
            )
            .execute()
        )
        return GmailMessagePage(
            messages=tuple(reversed(response.get("messages", []) or [])),
            next_page_token=str(response.get("nextPageToken") or "").strip(),
        )

    def get_raw_message(self, message_id: str) -> dict[str, Any]:
        return (
            self.service.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )

    def get_raw_thread(self, thread_id: str) -> list[dict[str, Any]]:
        response = (
            self.service.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute()
        )
        return list(response.get("messages", []) or [])

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute()
        )
        data = str(response.get("data") or "")
        return urlsafe_b64decode(data + "=" * (-len(data) % 4))
