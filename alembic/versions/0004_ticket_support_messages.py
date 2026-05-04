"""ticket support message ids

Revision ID: 0004_ticket_support_messages
Revises: 0003_ticket_maintenance
Create Date: 2026-05-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_ticket_support_messages"
down_revision = "0003_ticket_maintenance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.add_column(sa.Column("card_message_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("control_message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("tickets") as batch_op:
        batch_op.drop_column("control_message_id")
        batch_op.drop_column("card_message_id")
