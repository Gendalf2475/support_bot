"""waiting user reminders and auto-close warnings

Revision ID: 0008_waiting_user_and_warnings
Revises: 0007_omnichannel_platforms
Create Date: 2026-05-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_waiting_user_and_warnings"
down_revision = "0007_omnichannel_platforms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum("open", "closed", "cancelled", name="ticket_status", native_enum=False),
            type_=sa.Enum("open", "in_progress", "waiting_user", "closed", "cancelled", name="ticket_status", native_enum=False),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("last_support_message_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_user_reply_reminded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("auto_close_warning_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE tickets SET status = 'open' WHERE status IN ('in_progress', 'waiting_user')")
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("auto_close_warning_sent_at")
        batch_op.drop_column("last_user_reply_reminded_at")
        batch_op.drop_column("last_support_message_at")
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum("open", "in_progress", "waiting_user", "closed", "cancelled", name="ticket_status", native_enum=False),
            type_=sa.Enum("open", "closed", "cancelled", name="ticket_status", native_enum=False),
            existing_nullable=False,
        )
