from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from pungmail.adapters.discord.webhook import DiscordTransport, DiscordWebhookTransport
from pungmail.config import Settings, get_settings
from pungmail.repositories.database import session_scope
from pungmail.repositories.models import BusinessCase, DiscordMapping, DiscordOutbox
from pungmail.services.cards import RenderedCard


def queue_card(
    card: RenderedCard,
    *,
    source_gmail_message_id: str,
    settings: Settings | None = None,
) -> str:
    active = settings or get_settings()
    with session_scope() as session:
        case = session.get(BusinessCase, card.business_case_id)
        if case is None:
            raise LookupError(f"business case not found: {card.business_case_id}")
        mapping = session.scalar(
            select(DiscordMapping).where(DiscordMapping.business_case_id == case.id)
        )
        previous_preview = session.scalar(
            select(DiscordOutbox)
            .where(DiscordOutbox.business_case_id == case.id)
            .order_by(DiscordOutbox.created_at_utc.desc())
            .limit(1)
        )
        operation = "REPLACE" if mapping or previous_preview else "CREATE"
        identity = f"{case.id}:{case.current_revision}:{card.channel_key}:{card.body_sha256}"
        idempotency_key = sha256(identity.encode("utf-8")).hexdigest()
        existing = session.scalar(
            select(DiscordOutbox).where(DiscordOutbox.idempotency_key == idempotency_key)
        )
        if existing:
            return existing.id
        status = "PREVIEWED" if active.discord_mode.upper() == "PREVIEW" else "PENDING"
        if mapping and mapping.channel_key == card.channel_key and mapping.body_sha256 == card.body_sha256:
            status = "SKIPPED"
        row = DiscordOutbox(
            business_case_id=case.id,
            operation=operation,
            target_channel_key=card.channel_key,
            body_path=card.body_path,
            body_sha256=card.body_sha256,
            source_gmail_message_id=source_gmail_message_id,
            idempotency_key=idempotency_key,
            status=status,
            previous_channel_key=(
                mapping.channel_key
                if mapping
                else previous_preview.target_channel_key if previous_preview else None
            ),
            previous_message_id=(
                mapping.message_id
                if mapping
                else f"PREVIEW:{previous_preview.id}" if previous_preview else None
            ),
            previous_body_path=(
                mapping.last_body_path
                if mapping
                else previous_preview.body_path if previous_preview else None
            ),
        )
        session.add(row)
        session.flush()
        return row.id


def _load_body(settings: Settings, path_value: str) -> str:
    path = Path(path_value)
    if not path.is_absolute():
        path = settings.project_root / path
    return path.read_text(encoding="utf-8")


def _mark_delete_complete(outbox_id: str) -> None:
    with session_scope() as session:
        row = session.get(DiscordOutbox, outbox_id)
        if row:
            row.deletion_completed = True
            row.status = "REPOST_PENDING"
            row.updated_at_utc = datetime.now(UTC)


def _mark_failure(outbox_id: str, error: Exception, settings: Settings) -> None:
    with session_scope() as session:
        row = session.get(DiscordOutbox, outbox_id)
        if row is None:
            return
        row.attempt_count += 1
        row.last_error = f"{type(error).__name__}: {error}"[:4000]
        if row.deletion_completed:
            row.status = "REPOST_PENDING"
        elif row.attempt_count >= settings.outbox_max_attempts:
            row.status = "FAILED"
        else:
            row.status = "RETRY_WAIT"
        row.next_attempt_at_utc = datetime.now(UTC) + timedelta(
            seconds=min(3600, 30 * (2 ** min(row.attempt_count, 6)))
        )


def process_outbox_item(
    outbox_id: str,
    *,
    transport: DiscordTransport | None = None,
    settings: Settings | None = None,
) -> str:
    active = settings or get_settings()
    if active.discord_mode.upper() != "LIVE_TEST":
        raise RuntimeError("Discord network dispatch is allowed only in LIVE_TEST mode")
    client = transport or DiscordWebhookTransport(active)
    with session_scope() as session:
        row = session.get(DiscordOutbox, outbox_id)
        if row is None:
            raise LookupError(f"outbox item not found: {outbox_id}")
        if row.status in ("COMPLETED", "SKIPPED", "PREVIEWED"):
            return row.status
        snapshot = {
            "business_case_id": row.business_case_id,
            "operation": row.operation,
            "target_channel_key": row.target_channel_key,
            "body_path": row.body_path,
            "body_sha256": row.body_sha256,
            "source_gmail_message_id": row.source_gmail_message_id,
            "previous_channel_key": row.previous_channel_key,
            "previous_message_id": row.previous_message_id,
            "deletion_completed": row.deletion_completed,
        }
    try:
        body = _load_body(active, str(snapshot["body_path"]))
        if (
            snapshot["operation"] == "REPLACE"
            and snapshot["previous_message_id"]
            and not snapshot["deletion_completed"]
        ):
            client.delete(
                str(snapshot["previous_channel_key"]),
                str(snapshot["previous_message_id"]),
            )
            _mark_delete_complete(outbox_id)
        message_id = client.send(str(snapshot["target_channel_key"]), body)
        confirmed = client.fetch(str(snapshot["target_channel_key"]), message_id)
        if sha256(confirmed.encode("utf-8")).hexdigest() != snapshot["body_sha256"]:
            raise RuntimeError("Discord message verification hash mismatch")
        with session_scope() as session:
            row = session.get(DiscordOutbox, outbox_id)
            mapping = session.scalar(
                select(DiscordMapping).where(
                    DiscordMapping.business_case_id == snapshot["business_case_id"]
                )
            )
            if mapping is None:
                mapping = DiscordMapping(
                    business_case_id=str(snapshot["business_case_id"]),
                    channel_key=str(snapshot["target_channel_key"]),
                    message_id=message_id,
                    body_sha256=str(snapshot["body_sha256"]),
                    last_body_path=str(snapshot["body_path"]),
                    last_source_gmail_message_id=str(snapshot["source_gmail_message_id"]),
                )
                session.add(mapping)
            else:
                mapping.channel_key = str(snapshot["target_channel_key"])
                mapping.message_id = message_id
                mapping.body_sha256 = str(snapshot["body_sha256"])
                mapping.last_body_path = str(snapshot["body_path"])
                mapping.last_source_gmail_message_id = str(snapshot["source_gmail_message_id"])
            if row:
                row.status = "COMPLETED"
                row.attempt_count += 1
                row.last_error = None
                row.next_attempt_at_utc = None
        return "COMPLETED"
    except Exception as exc:
        _mark_failure(outbox_id, exc, active)
        raise


def process_pending_outbox(
    *,
    limit: int = 20,
    transport: DiscordTransport | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    active = settings or get_settings()
    now = datetime.now(UTC)
    with session_scope() as session:
        rows = session.scalars(
            select(DiscordOutbox)
            .where(
                DiscordOutbox.status.in_(["PENDING", "RETRY_WAIT", "REPOST_PENDING"]),
                (DiscordOutbox.next_attempt_at_utc.is_(None))
                | (DiscordOutbox.next_attempt_at_utc <= now),
            )
            .order_by(DiscordOutbox.created_at_utc)
            .limit(limit)
        ).all()
        ids = [row.id for row in rows]
    result = {"completed": 0, "failed": 0}
    for outbox_id in ids:
        try:
            process_outbox_item(outbox_id, transport=transport, settings=active)
            result["completed"] += 1
        except Exception:
            result["failed"] += 1
    return result
