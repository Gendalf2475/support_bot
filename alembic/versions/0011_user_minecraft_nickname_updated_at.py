"""add user minecraft nickname updated timestamp

Revision ID: 0011_user_minecraft_nickname_updated_at
Revises: 0010_ticket_answer_minecraft_lookup
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_user_minecraft_nickname_updated_at"
down_revision = "0010_ticket_answer_minecraft_lookup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("minecraft_nickname_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("minecraft_nickname_updated_at")
