"""add user minecraft nickname

Revision ID: 0009_user_minecraft_nickname
Revises: 0008_waiting_user_and_warnings
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_user_minecraft_nickname"
down_revision = "0008_waiting_user_and_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("minecraft_nickname", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("minecraft_nickname")
