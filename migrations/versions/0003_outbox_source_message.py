"""Track the Gmail source used to render each Discord outbox item.

Revision ID: 0003_outbox_source
Revises: 0002_issue_two
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_outbox_source"
down_revision: Union[str, Sequence[str], None] = "0002_issue_two"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in sa.inspect(bind).get_columns("discord_outbox")}
    if "source_gmail_message_id" not in columns:
        with op.batch_alter_table("discord_outbox") as batch:
            batch.add_column(
                sa.Column("source_gmail_message_id", sa.String(128), nullable=False, server_default="")
            )


def downgrade() -> None:
    with op.batch_alter_table("discord_outbox") as batch:
        batch.drop_column("source_gmail_message_id")
