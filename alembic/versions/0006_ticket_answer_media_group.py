"""ticket answer media group metadata

Revision ID: 0006_ticket_answer_media_group
Revises: 0005_ticket_answer_media
Create Date: 2026-05-05 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_ticket_answer_media_group"
down_revision = "0005_ticket_answer_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("ticket_answer_media") as batch_op:
        batch_op.add_column(sa.Column("media_group_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"))

    op.execute("UPDATE ticket_answer_media SET sort_order = id WHERE sort_order = 0")

    with op.batch_alter_table("ticket_answer_media") as batch_op:
        batch_op.alter_column("sort_order", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("ticket_answer_media") as batch_op:
        batch_op.drop_column("sort_order")
        batch_op.drop_column("media_group_id")
