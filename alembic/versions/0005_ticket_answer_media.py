"""ticket answer media files

Revision ID: 0005_ticket_answer_media
Revises: 0004_ticket_support_messages
Create Date: 2026-05-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_ticket_answer_media"
down_revision = "0004_ticket_support_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_answer_media",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "ticket_answer_id",
            sa.Integer(),
            sa.ForeignKey("ticket_answers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("file_id", sa.String(length=512), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_ticket_answer_media_ticket_answer_id", "ticket_answer_media", ["ticket_answer_id"])

    connection = op.get_bind()
    ticket_answers = connection.execute(
        sa.text(
            "SELECT id, file_id, media_type, caption, created_at "
            "FROM ticket_answers "
            "WHERE file_id IS NOT NULL"
        )
    )
    for answer in ticket_answers:
        connection.execute(
            sa.text(
                "INSERT INTO ticket_answer_media "
                "(ticket_answer_id, file_id, media_type, caption, created_at) "
                "VALUES (:ticket_answer_id, :file_id, :media_type, :caption, :created_at)"
            ),
            {
                "ticket_answer_id": answer.id,
                "file_id": answer.file_id,
                "media_type": answer.media_type or "media",
                "caption": answer.caption,
                "created_at": answer.created_at,
            },
        )


def downgrade() -> None:
    op.drop_index("ix_ticket_answer_media_ticket_answer_id", table_name="ticket_answer_media")
    op.drop_table("ticket_answer_media")
