"""ticket system

Revision ID: 0002_ticket_system
Revises: 0001_initial
Create Date: 2026-05-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_ticket_system"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("form_id", sa.String(length=128), nullable=False),
        sa.Column("form_title", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "closed", "cancelled", name="ticket_status", native_enum=False),
            nullable=False,
        ),
        sa.Column("topic_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_by_telegram_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_topic_id", "tickets", ["topic_id"])
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])

    op.create_table(
        "ticket_answers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.String(length=128), nullable=False),
        sa.Column("question_text", sa.Text(), nullable=False),
        sa.Column("answer_type", sa.String(length=32), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=True),
        sa.Column("file_id", sa.String(length=512), nullable=True),
        sa.Column("media_type", sa.String(length=32), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_answers_ticket_id", "ticket_answers", ["ticket_id"])

    with op.batch_alter_table("message_maps") as batch_op:
        batch_op.add_column(sa.Column("ticket_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_message_maps_ticket_id_tickets",
            "tickets",
            ["ticket_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_message_maps_ticket_id", ["ticket_id"])


def downgrade() -> None:
    with op.batch_alter_table("message_maps") as batch_op:
        batch_op.drop_index("ix_message_maps_ticket_id")
        batch_op.drop_constraint("fk_message_maps_ticket_id_tickets", type_="foreignkey")
        batch_op.drop_column("ticket_id")

    op.drop_index("ix_ticket_answers_ticket_id", table_name="ticket_answers")
    op.drop_table("ticket_answers")

    op.drop_index("ix_tickets_user_id", table_name="tickets")
    op.drop_index("ix_tickets_topic_id", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_table("tickets")
