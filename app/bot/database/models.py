from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class MessageDirection(StrEnum):
    USER_TO_SUPPORT = "user_to_support"
    SUPPORT_TO_USER = "support_to_user"
    TICKET_FORM_MEDIA = "ticket_form_media"


class TicketStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    topic_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_user_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class MessageMap(Base):
    __tablename__ = "message_maps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    ticket_id: Mapped[int | None] = mapped_column(
        ForeignKey("tickets.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    user_message_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    support_message_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
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
