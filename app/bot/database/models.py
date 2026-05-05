from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MessageDirection(StrEnum):
    USER_TO_SUPPORT = "user_to_support"
    SUPPORT_TO_USER = "support_to_user"
    TICKET_FORM_MEDIA = "ticket_form_media"


class Platform(StrEnum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    VK = "vk"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING_USER = "waiting_user"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("platform", "platform_user_id", name="uq_users_platform_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default=Platform.TELEGRAM.value)
    platform_user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    topic_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    minecraft_nickname: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )

    messages: Mapped[list[MessageMap]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    tickets: Mapped[list[Ticket]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default=Platform.TELEGRAM.value)
    form_id: Mapped[str] = mapped_column(String(128), nullable=False)
    form_title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        Enum(
            TicketStatus,
            name="ticket_status",
            native_enum=False,
            values_callable=lambda enum_class: [item.value for item in enum_class],
        ),
        index=True,
        nullable=False,
        default=TicketStatus.OPEN,
    )
    topic_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    card_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    control_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_user_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_support_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_user_reply_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_close_warning_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    user: Mapped[User] = relationship(back_populates="tickets")
    answers: Mapped[list[TicketAnswer]] = relationship(
        back_populates="ticket",
        cascade="all, delete-orphan",
        order_by="TicketAnswer.id",
    )
    messages: Mapped[list[MessageMap]] = relationship(back_populates="ticket")


class TicketAnswer(Base):
    __tablename__ = "ticket_answers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    question_id: Mapped[str] = mapped_column(String(128), nullable=False)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_type: Mapped[str] = mapped_column(String(32), nullable=False)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    ticket: Mapped[Ticket] = relationship(back_populates="answers")
    media_files: Mapped[list[TicketAnswerMedia]] = relationship(
        back_populates="answer",
        cascade="all, delete-orphan",
        order_by="TicketAnswerMedia.id",
    )


class TicketAnswerMedia(Base):
    __tablename__ = "ticket_answer_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_answer_id: Mapped[int] = mapped_column(
        ForeignKey("ticket_answers.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    file_id: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(32), nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    answer: Mapped[TicketAnswer] = relationship(back_populates="media_files")


class MessageMap(Base):
    __tablename__ = "message_maps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    platform: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default=Platform.TELEGRAM.value)
    platform_message_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    telegram_support_message_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    user_message_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    support_message_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    topic_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(
        Enum(
            MessageDirection,
            name="message_direction",
            native_enum=False,
            values_callable=lambda enum_class: [item.value for item in enum_class],
        ),
        index=True,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="messages")
    ticket: Mapped[Ticket | None] = relationship(back_populates="messages")
