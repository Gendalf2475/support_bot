"""ticket maintenance fields

Revision ID: 0003_ticket_maintenance
Revises: 0002_ticket_system
Create Date: 2026-05-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_ticket_maintenance"
down_revision = "0002_ticket_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("close_reason", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("last_user_message_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_reminded_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        "UPDATE tickets "
        "SET last_user_message_at = created_at "
        "WHERE status = 'open' AND last_user_message_at IS NULL"
    )

    with op.batch_alter_table("ticket_answers") as batch_op:
        batch_op.add_column(
            sa.Column(
                "skipped",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("ticket_answers") as batch_op:
        batch_op.drop_column("skipped")

    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("last_reminded_at")
        batch_op.drop_column("last_user_message_at")
        batch_op.drop_column("close_reason")
