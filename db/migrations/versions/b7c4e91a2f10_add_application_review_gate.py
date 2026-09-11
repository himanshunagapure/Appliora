"""Add application review_decision / reviewed_at for Phase 8.

Revision ID: b7c4e91a2f10
Revises: 3801b0d3a831
Create Date: 2026-09-10 22:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c4e91a2f10"
down_revision: Union[str, Sequence[str], None] = "3801b0d3a831"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "applications",
        sa.Column("review_decision", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "applications",
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("applications", "reviewed_at")
    op.drop_column("applications", "review_decision")
