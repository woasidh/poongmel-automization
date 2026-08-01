"""이슈 1 관찰 플랫폼 초기 스키마.

Revision ID: 0001_issue_one
Revises:
Create Date: 2026-08-01
"""
from typing import Sequence, Union

from alembic import op

from pungmail.repositories.models import Base


revision: str = "0001_issue_one"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
