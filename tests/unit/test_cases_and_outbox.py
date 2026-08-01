from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from pungmail.config import Settings
from pungmail.domain.decisions import MailDecision
from pungmail.repositories.cases import apply_decision
from pungmail.repositories.database import session_scope
from pungmail.repositories.mail_store import enqueue_messages
from pungmail.repositories.models import (
    BusinessCase,
    CaseHistory,
    DiscordMapping,
    DiscordOutbox,
    MailEvent,
)
from pungmail.repositories.tracking import create_workflow_run
from pungmail.services.cards import render_case
from pungmail.services.outbox import process_outbox_item, queue_card


def decision(
    message_id: str,
    *,
    completed: bool = False,
    subject: str = "VC-IP 샘플 요청",
) -> MailDecision:
    return MailDecision.model_validate(
        {
            "category": "샘플자료견적",
            "case_action": "UPDATE" if message_id != "message-1" else "CREATE",
            "case_lookup_keys": {
                "original_message_id": message_id,
                "thread_id": "thread-1",
                "company_key": "customer-a",
                "po_numbers": [],
                "rw_numbers": [],
                "si_numbers": [],
                "item_component_keys": ["VC-IP|SAMPLE"],
            },
            "company": "고객사 A",
            "subject": subject,
            "summary": "VC-IP 샘플 요청 처리",
            "missing_fields": [],
            "evidence_refs": [f"gmail:{message_id}"],
            "category_payload": {
                "payload_type": "SAMPLE_DOCUMENT_QUOTE",
                "items": [
                    {
                        "raw_product_name": "VC-IP",
                        "item_code": "30500314",
                        "spec": "5KG",
                        "quantity": "1",
                        "unit": "EA",
                    }
                ],
                "components": [
                    {
                        "component_type": "SAMPLE",
                        "label": "VC-IP 샘플",
                        "completed": completed,
                        "evidence_ref": f"gmail:{message_id}",
                    }
                ],
                "end_user": None,
                "destination": "서울",
                "recipient": "홍길동",
                "contact": None,
            },
        }
    )


def event_id(message_id: str) -> str:
    with session_scope() as session:
        row = session.scalar(select(MailEvent).where(MailEvent.gmail_message_id == message_id))
        assert row is not None
        return row.id


def test_exact_resolver_creates_then_updates_one_case(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "message-1", "threadId": "thread-1"}], run_id)
    first = apply_decision(event_id("message-1"), decision("message-1"))
    enqueue_messages([{"id": "message-2", "threadId": "thread-1"}], run_id)
    second = apply_decision(
        event_id("message-2"),
        decision("message-2", completed=True, subject="VC-IP 샘플 완료"),
    )

    assert first.event_action == "CREATE"
    assert second.event_action == "UPDATE"
    assert second.business_case_id == first.business_case_id
    assert second.revision == 2
    assert second.status == "COMPLETED"
    with session_scope() as session:
        assert session.scalar(select(func.count()).select_from(BusinessCase)) == 1
        assert session.scalar(select(func.count()).select_from(CaseHistory)) == 2


class FakeDiscord:
    def __init__(self) -> None:
        self.messages: dict[tuple[str, str], str] = {}
        self.sent: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self.fail_next_send = False

    def send(self, channel_key: str, body: str) -> str:
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("simulated send failure")
        message_id = f"discord-{len(self.sent) + 1}"
        self.sent.append((channel_key, body))
        self.messages[(channel_key, message_id)] = body
        return message_id

    def fetch(self, channel_key: str, message_id: str) -> str:
        return self.messages[(channel_key, message_id)]

    def delete(self, channel_key: str, message_id: str) -> None:
        self.deleted.append((channel_key, message_id))
        self.messages.pop((channel_key, message_id), None)


def live_settings(base: Settings) -> Settings:
    values = base.model_dump()
    values["discord_mode"] = "LIVE_TEST"
    return Settings(**values)


def test_update_deletes_then_reposts_and_recovers_without_second_delete(
    isolated_settings,
) -> None:
    settings = live_settings(isolated_settings)
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "message-1", "threadId": "thread-1"}], run_id)
    first = apply_decision(event_id("message-1"), decision("message-1"))
    first_card = render_case(first.business_case_id, settings=settings)
    first_outbox = queue_card(
        first_card, source_gmail_message_id="message-1", settings=settings
    )
    discord = FakeDiscord()
    assert process_outbox_item(first_outbox, transport=discord, settings=settings) == "COMPLETED"

    enqueue_messages([{"id": "message-2", "threadId": "thread-1"}], run_id)
    updated = apply_decision(
        event_id("message-2"), decision("message-2", completed=True)
    )
    updated_card = render_case(updated.business_case_id, settings=settings)
    assert updated_card.channel_key == "completed"
    assert "~~VC\\-IP 샘플~~" in updated_card.body
    replace_id = queue_card(
        updated_card, source_gmail_message_id="message-2", settings=settings
    )
    discord.fail_next_send = True
    with pytest.raises(RuntimeError, match="simulated"):
        process_outbox_item(replace_id, transport=discord, settings=settings)
    with session_scope() as session:
        failed = session.get(DiscordOutbox, replace_id)
        assert failed is not None
        assert failed.status == "REPOST_PENDING"
        assert failed.deletion_completed is True

    assert process_outbox_item(replace_id, transport=discord, settings=settings) == "COMPLETED"
    assert len(discord.deleted) == 1
    with session_scope() as session:
        mapping = session.scalar(
            select(DiscordMapping).where(
                DiscordMapping.business_case_id == updated.business_case_id
            )
        )
        assert mapping is not None
        assert mapping.channel_key == "completed"
        assert mapping.last_source_gmail_message_id == "message-2"


def test_preview_mode_never_dispatches_network(isolated_settings) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "message-1", "threadId": "thread-1"}], run_id)
    applied = apply_decision(event_id("message-1"), decision("message-1"))
    card = render_case(applied.business_case_id, settings=isolated_settings)
    outbox_id = queue_card(
        card, source_gmail_message_id="message-1", settings=isolated_settings
    )
    with session_scope() as session:
        row = session.get(DiscordOutbox, outbox_id)
        assert row is not None and row.status == "PREVIEWED"
    with pytest.raises(RuntimeError, match="LIVE_TEST"):
        process_outbox_item(outbox_id, transport=FakeDiscord(), settings=isolated_settings)
