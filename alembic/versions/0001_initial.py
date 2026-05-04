"""initial tables

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-03 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"], unique=True)
    op.create_index("ix_users_topic_id", "users", ["topic_id"], unique=True)

    op.create_table(
        "message_maps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_message_id", sa.Integer(), nullable=False),
        sa.Column("support_message_id", sa.Integer(), nullable=False),
        sa.Column("topic_id", sa.Integer(), nullable=False),
        sa.Column(
            "direction",
            sa.Enum("user_to_support", "support_to_user", name="message_direction", native_enum=False),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_message_maps_direction", "message_maps", ["direction"])
    op.create_index("ix_message_maps_support_message_id", "message_maps", ["support_message_id"])
    op.create_index("ix_message_maps_topic_id", "message_maps", ["topic_id"])
    op.create_index("ix_message_maps_user_id", "message_maps", ["user_id"])
    op.create_index("ix_message_maps_user_message_id", "message_maps", ["user_message_id"])


def downgrade() -> None:
    op.drop_index("ix_message_maps_user_message_id", table_name="message_maps")
    op.drop_index("ix_message_maps_user_id", table_name="message_maps")
    op.drop_index("ix_message_maps_topic_id", table_name="message_maps")
    op.drop_index("ix_message_maps_support_message_id", table_name="message_maps")
    op.drop_index("ix_message_maps_direction", table_name="message_maps")
    op.drop_table("message_maps")
    op.drop_index("ix_users_topic_id", table_name="users")
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_table("users")
