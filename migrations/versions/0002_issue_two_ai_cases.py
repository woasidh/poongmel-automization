"""Issue 2 AI decisions, business cases, requests, and Discord outbox.

Revision ID: 0002_issue_two
Revises: 0001_issue_one
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from pungmail.repositories.models import Base


revision: str = "0002_issue_two"
down_revision: Union[str, Sequence[str], None] = "0001_issue_one"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_TABLES = (
    "ai_decisions",
    "business_cases",
    "case_lookup_keys",
    "case_history",
    "request_cases",
    "request_items",
    "request_components",
    "discord_mappings",
    "discord_outbox",
)


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in NEW_TABLES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)
    existing_columns = {item["name"] for item in sa.inspect(bind).get_columns("mail_events")}
    if "business_case_id" in existing_columns:
        return
    with op.batch_alter_table("mail_events") as batch:
        batch.add_column(sa.Column("business_case_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("ai_decision_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("event_action", sa.String(32), nullable=True))
        batch.add_column(sa.Column("applied_revision", sa.Integer(), nullable=True))
        batch.create_index("ix_mail_events_business_case_id", ["business_case_id"])
        batch.create_index("ix_mail_events_ai_decision_id", ["ai_decision_id"])
        batch.create_foreign_key(
            "fk_mail_events_business_case", "business_cases", ["business_case_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_mail_events_ai_decision", "ai_decisions", ["ai_decision_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("mail_events") as batch:
        batch.drop_constraint("fk_mail_events_ai_decision", type_="foreignkey")
        batch.drop_constraint("fk_mail_events_business_case", type_="foreignkey")
        batch.drop_index("ix_mail_events_ai_decision_id")
        batch.drop_index("ix_mail_events_business_case_id")
        batch.drop_column("applied_revision")
        batch.drop_column("event_action")
        batch.drop_column("ai_decision_id")
        batch.drop_column("business_case_id")
    bind = op.get_bind()
    for table_name in reversed(NEW_TABLES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)
