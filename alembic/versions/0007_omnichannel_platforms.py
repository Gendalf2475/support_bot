"""omnichannel platform fields

Revision ID: 0007_omnichannel_platforms
Revises: 0006_ticket_answer_media_group
Create Date: 2026-05-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_omnichannel_platforms"
down_revision = "0006_ticket_answer_media_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("platform", sa.String(length=32), nullable=False, server_default="telegram"))
        batch_op.add_column(sa.Column("platform_user_id", sa.String(length=255), nullable=True))
        batch_op.alter_column("telegram_id", nullable=True)

    op.execute("UPDATE users SET platform_user_id = CAST(telegram_id AS VARCHAR) WHERE platform_user_id IS NULL")

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("platform", server_default=None)
        batch_op.alter_column("platform_user_id", nullable=False)
        batch_op.create_index("ix_users_platform", ["platform"])
        batch_op.create_index("ix_users_platform_user_id", ["platform_user_id"])
        batch_op.create_unique_constraint("uq_users_platform_user_id", ["platform", "platform_user_id"])

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("platform", sa.String(length=32), nullable=False, server_default="telegram"))
        batch_op.create_index("ix_tickets_platform", ["platform"])

    op.execute(
        "UPDATE tickets SET platform = ("
        "SELECT users.platform FROM users WHERE users.id = tickets.user_id"
        ") WHERE platform IS NULL OR platform = 'telegram'"
    )

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column("platform", server_default=None)

    with op.batch_alter_table("message_maps") as batch_op:
        batch_op.add_column(sa.Column("platform", sa.String(length=32), nullable=False, server_default="telegram"))
        batch_op.add_column(sa.Column("platform_message_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("telegram_support_message_id", sa.Integer(), nullable=True))
        batch_op.alter_column("user_message_id", nullable=True)
        batch_op.alter_column("support_message_id", nullable=True)
        batch_op.create_index("ix_message_maps_platform", ["platform"])
        batch_op.create_index("ix_message_maps_platform_message_id", ["platform_message_id"])
        batch_op.create_index("ix_message_maps_telegram_support_message_id", ["telegram_support_message_id"])

    op.execute(
        "UPDATE message_maps SET "
        "platform_message_id = CAST(user_message_id AS VARCHAR), "
        "telegram_support_message_id = support_message_id "
        "WHERE platform_message_id IS NULL"
    )

    with op.batch_alter_table("message_maps") as batch_op:
        batch_op.alter_column("platform", server_default=None)

    with op.batch_alter_table("ticket_answer_media") as batch_op:
        batch_op.add_column(sa.Column("file_url", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("filename", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("mime_type", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ticket_answer_media") as batch_op:
        batch_op.drop_column("mime_type")
        batch_op.drop_column("filename")
        batch_op.drop_column("file_url")

    with op.batch_alter_table("message_maps") as batch_op:
        batch_op.alter_column("platform", server_default="telegram")
        batch_op.drop_index("ix_message_maps_telegram_support_message_id")
        batch_op.drop_index("ix_message_maps_platform_message_id")
        batch_op.drop_index("ix_message_maps_platform")
        batch_op.alter_column("support_message_id", nullable=False)
        batch_op.alter_column("user_message_id", nullable=False)
        batch_op.drop_column("telegram_support_message_id")
        batch_op.drop_column("platform_message_id")
        batch_op.drop_column("platform")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_index("ix_tickets_platform")
        batch_op.drop_column("platform")

    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_constraint("uq_users_platform_user_id", type_="unique")
        batch_op.drop_index("ix_users_platform_user_id")
        batch_op.drop_index("ix_users_platform")
        batch_op.alter_column("telegram_id", nullable=False)
        batch_op.drop_column("platform_user_id")
        batch_op.drop_column("platform")
