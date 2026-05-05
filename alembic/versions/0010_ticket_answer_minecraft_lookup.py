"""add ticket answer minecraft lookup fields

Revision ID: 0010_ticket_answer_minecraft_lookup
Revises: 0009_user_minecraft_nickname
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_ticket_answer_minecraft_lookup"
down_revision = "0009_user_minecraft_nickname"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_answers") as batch_op:
        batch_op.add_column(sa.Column("profile_field", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_nickname", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_exists", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_uuid", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_online", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_source", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("minecraft_lookup_error", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ticket_answers") as batch_op:
        batch_op.drop_column("minecraft_lookup_error")
        batch_op.drop_column("minecraft_lookup_source")
        batch_op.drop_column("minecraft_lookup_online")
        batch_op.drop_column("minecraft_lookup_uuid")
        batch_op.drop_column("minecraft_lookup_exists")
        batch_op.drop_column("minecraft_lookup_nickname")
        batch_op.drop_column("profile_field")
