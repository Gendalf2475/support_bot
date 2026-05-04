from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReplyKeyboardRemove
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.database.models import Ticket, TicketAnswer, TicketAnswerMedia, TicketStatus, User, utcnow
from app.bot.services.ticket_formatter import TicketFormatter
from app.bot.services.ticket_form_service import TicketForm


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseReason:
    code: str
    label: str
    button_text: str


CLOSE_REASONS: dict[str, CloseReason] = {
    "resolved": CloseReason("resolved", "Решено", "✅ Решено"),
    "no_user_response": CloseReason("no_user_response", "Нет ответа от пользователя", "👤 Нет ответа от пользователя"),
    "duplicate": CloseReason("duplicate", "Дубликат", "🔁 Дубликат"),
    "rule_violation": CloseReason("rule_violation", "Нарушение правил", "⛔ Нарушение правил"),
    "other": CloseReason("other", "Другая причина", "📝 Другая причина"),
    "auto_no_user_response": CloseReason(
        "auto_no_user_response",
        "Автоматически: нет ответа от пользователя",
        "Автоматически: нет ответа от пользователя",
    ),
}
MANUAL_CLOSE_REASON_CODES = ("resolved", "no_user_response", "duplicate", "rule_violation", "other")
AUTO_NO_USER_RESPONSE_REASON = "auto_no_user_response"


class TicketService:
    def __init__(self, session: AsyncSession, support_chat_id: int) -> None:
        self.session = session
        self.support_chat_id = support_chat_id

    async def get_open_ticket_by_user_id(self, user_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.user_id == user_id, Ticket.status == TicketStatus.OPEN)
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def get_open_ticket_by_topic_id(self, topic_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.topic_id == topic_id, Ticket.status == TicketStatus.OPEN)
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def get_ticket_by_id(self, ticket_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.id == ticket_id)
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
        )
        return await self.session.scalar(statement)

    async def create_open_ticket(
        self,
        user: User,
        form: TicketForm,
        answers: list[dict[str, Any]],
        topic_id: int,
    ) -> Ticket:
        now = utcnow()
        ticket = Ticket(
            user_id=user.id,
            form_id=form.id,
            form_title=form.admin_title,
            status=TicketStatus.OPEN,
            topic_id=topic_id,
            created_at=now,
            updated_at=now,
            last_user_message_at=now,
        )
        self.session.add(ticket)
        await self.session.flush()
        self._add_answers(ticket, form, answers)
        await self.session.flush()
        logger.info("Created open ticket id=%s telegram_id=%s topic_id=%s", ticket.id, user.telegram_id, topic_id)
        return ticket

    async def create_cancelled_ticket(
        self,
        user: User,
        form: TicketForm,
        answers: list[dict[str, Any]],
    ) -> Ticket:
        ticket = Ticket(
            user_id=user.id,
            form_id=form.id,
            form_title=form.admin_title,
            status=TicketStatus.CANCELLED,
            topic_id=user.topic_id,
        )
        self.session.add(ticket)
        await self.session.flush()
        self._add_answers(ticket, form, answers)
        await self.session.flush()
        logger.info("Created cancelled ticket id=%s telegram_id=%s", ticket.id, user.telegram_id)
        return ticket

    async def set_topic_id(self, ticket: Ticket, topic_id: int) -> None:
        ticket.topic_id = topic_id
        await self.session.flush()

    async def set_card_message_id(self, ticket: Ticket, message_id: int) -> None:
        ticket.card_message_id = message_id
        await self.session.flush()

    async def set_control_message_id(self, ticket: Ticket, message_id: int) -> None:
        ticket.control_message_id = message_id
        await self.session.flush()

    async def pin_ticket_card(self, bot: Bot, ticket: Ticket) -> None:
        if not ticket.card_message_id:
            return

        try:
            await bot.pin_chat_message(
                chat_id=self.support_chat_id,
                message_id=ticket.card_message_id,
                disable_notification=True,
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to pin ticket card ticket_id=%s topic_id=%s card_message_id=%s: %s",
                ticket.id,
                ticket.topic_id,
                ticket.card_message_id,
                error,
            )

    async def mark_user_activity(self, ticket: Ticket) -> None:
        ticket.last_user_message_at = utcnow()
        await self.session.flush()

    async def close_ticket(
        self,
        bot: Bot,
        ticket: Ticket,
        reason: str,
        closed_by_telegram_id: int | None = None,
        auto_close_after_days: int | None = None,
        ticket_forms: list[TicketForm] | None = None,
    ) -> bool:
        if ticket.status != TicketStatus.OPEN:
            return False

        if reason not in CLOSE_REASONS:
            raise ValueError(f"Unknown ticket close reason: {reason}")

        ticket.status = TicketStatus.CLOSED
        ticket.close_reason = reason
        ticket.closed_at = utcnow()
        ticket.closed_by_telegram_id = closed_by_telegram_id
        await self.session.flush()

        user = await self.session.get(User, ticket.user_id)
        topic_id = ticket.topic_id or (user.topic_id if user else None)
        topic_text = self.build_support_close_text(reason, auto_close_after_days)
        if topic_id:
            try:
                await bot.send_message(
                    chat_id=self.support_chat_id,
                    message_thread_id=topic_id,
                    text=topic_text,
                    reply_markup=ReplyKeyboardRemove(),
                )
            except TelegramAPIError as error:
                logger.error("Failed to send ticket closed notice ticket_id=%s topic_id=%s: %s", ticket.id, topic_id, error)

        if user:
            try:
                reply_markup = None
                if not user.blocked and ticket_forms:
                    from app.bot.keyboards import ticket_forms_reply_keyboard

                    reply_markup = ticket_forms_reply_keyboard(ticket_forms)

                await bot.send_message(
                    chat_id=user.telegram_id,
                    text=self.build_user_close_text(reason, auto_close_after_days),
                    reply_markup=reply_markup,
                )
            except TelegramAPIError as error:
                logger.error("Failed to notify user about closed ticket ticket_id=%s telegram_id=%s: %s", ticket.id, user.telegram_id, error)
                if topic_id:
                    await self.notify_support_about_user_notification_error(bot, topic_id)

        if user:
            await self.update_ticket_card(bot, ticket, user)
        await self.update_ticket_control_message(bot, ticket)

        logger.info(
            "Closed ticket id=%s by telegram_id=%s reason=%s",
            ticket.id,
            closed_by_telegram_id,
            reason,
        )
        return True

    async def send_due_reminders(
        self,
        bot: Bot,
        reminder_after_hours: int,
        reminder_interval_minutes: int,
    ) -> int:
        now = utcnow()
        opened_before = now - timedelta(hours=reminder_after_hours)
        reminded_before = now - timedelta(minutes=reminder_interval_minutes)
        statement = select(Ticket).where(
            Ticket.status == TicketStatus.OPEN,
            Ticket.topic_id.is_not(None),
            Ticket.created_at <= opened_before,
            or_(
                Ticket.last_reminded_at.is_(None),
                Ticket.last_reminded_at <= reminded_before,
            ),
        )
        tickets = list((await self.session.scalars(statement)).all())
        sent_count = 0
        for ticket in tickets:
            try:
                await bot.send_message(
                    chat_id=self.support_chat_id,
                    message_thread_id=ticket.topic_id,
                    text=(
                        f"⏰ Тикет открыт больше {reminder_after_hours} часов.\n"
                        "Пожалуйста, проверьте обращение или закройте тикет, если вопрос решён."
                    ),
                )
            except TelegramAPIError as error:
                logger.error("Failed to send ticket reminder ticket_id=%s topic_id=%s: %s", ticket.id, ticket.topic_id, error)
                continue

            ticket.last_reminded_at = now
            sent_count += 1

        await self.session.flush()
        return sent_count

    async def auto_close_inactive_tickets(
        self,
        bot: Bot,
        auto_close_after_days: int,
        ticket_forms: list[TicketForm] | None = None,
    ) -> int:
        now = utcnow()
        inactive_before = now - timedelta(days=auto_close_after_days)
        statement = (
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.OPEN,
                or_(
                    Ticket.last_user_message_at <= inactive_before,
                    and_(
                        Ticket.last_user_message_at.is_(None),
                        Ticket.created_at <= inactive_before,
                    ),
                ),
            )
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
        )
        tickets = list((await self.session.scalars(statement)).all())
        closed_count = 0
        for ticket in tickets:
            try:
                closed = await self.close_ticket(
                    bot=bot,
                    ticket=ticket,
                    reason=AUTO_NO_USER_RESPONSE_REASON,
                    closed_by_telegram_id=None,
                    auto_close_after_days=auto_close_after_days,
                    ticket_forms=ticket_forms,
                )
            except Exception as error:
                logger.exception("Failed to auto-close ticket_id=%s: %s", ticket.id, error)
                continue
            if closed:
                closed_count += 1

        return closed_count

    def _add_answers(self, ticket: Ticket, form: TicketForm, answers: list[dict[str, Any]]) -> None:
        answers_by_question: dict[str, list[dict[str, Any]]] = {}
        for answer in answers:
            question_id = answer.get("question_id")
            if question_id:
                answers_by_question.setdefault(str(question_id), []).append(answer)

        for question in form.questions:
            for answer in answers_by_question.get(question.id, []):
                media_files = self.extract_media_files(answer)
                first_media = media_files[0] if media_files else None
                ticket_answer = TicketAnswer(
                    ticket_id=ticket.id,
                    question_id=question.id,
                    question_text=question.text,
                    answer_type=str(answer.get("answer_type") or "text"),
                    answer_text=answer.get("answer_text"),
                    file_id=first_media.get("file_id") if first_media else answer.get("file_id"),
                    media_type=first_media.get("media_type") if first_media else answer.get("media_type"),
                    caption=first_media.get("caption") if first_media else answer.get("caption"),
                    skipped=bool(answer.get("skipped", False)),
                )
                for media_index, media in enumerate(media_files):
                    ticket_answer.media_files.append(
                        TicketAnswerMedia(
                            file_id=str(media.get("file_id") or ""),
                            media_type=str(media.get("media_type") or "media"),
                            caption=media.get("caption"),
                            media_group_id=media.get("media_group_id"),
                            sort_order=TicketService._media_sort_order(media, media_index),
                        )
                    )
                self.session.add(ticket_answer)

    async def update_ticket_card(self, bot: Bot, ticket: Ticket, user: User) -> None:
        if not ticket.card_message_id:
            return

        close_reason_label = None
        if ticket.close_reason:
            close_reason_label = self.get_close_reason_label(ticket.close_reason)

        try:
            await bot.edit_message_text(
                chat_id=self.support_chat_id,
                message_id=ticket.card_message_id,
                text=TicketFormatter.build_ticket_parts(ticket, user, close_reason_label)[0],
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to update ticket card ticket_id=%s topic_id=%s card_message_id=%s: %s",
                ticket.id,
                ticket.topic_id,
                ticket.card_message_id,
                error,
            )

    async def update_ticket_control_message(self, bot: Bot, ticket: Ticket) -> None:
        if not ticket.control_message_id:
            return

        try:
            await bot.edit_message_text(
                chat_id=self.support_chat_id,
                message_id=ticket.control_message_id,
                text=TicketFormatter.build_closed_control_text(ticket.id),
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to update ticket control message ticket_id=%s topic_id=%s control_message_id=%s: %s",
                ticket.id,
                ticket.topic_id,
                ticket.control_message_id,
                error,
            )

    @staticmethod
    def build_user_summary_text(form: TicketForm, answers: list[dict[str, Any]]) -> str:
        lines = [
            "Проверьте данные тикета:",
            "",
            f"Тип обращения: {form.title}",
            "",
        ]
        lines.extend(TicketService._format_answer_lines(form, answers, for_admin=False))
        lines.extend(["", "Отправить тикет?"])
        return TicketService.limit_text("\n".join(lines))

    @staticmethod
    def build_admin_ticket_text(form: TicketForm, user: User, answers: list[dict[str, Any]]) -> str:
        username = f"@{str(user.username).strip('@')}" if user.username else "нет username"
        full_name = user.full_name or "не указано"
        lines = [
            "🟣 Новый тикет",
            "",
            f"Тип: {form.admin_title}",
            f"Пользователь: {username}",
            f"ID: {user.telegram_id}",
            f"Имя: {full_name}",
            "",
            "Данные формы:",
            "",
        ]
        lines.extend(TicketService._format_answer_lines(form, answers, for_admin=True))
        return TicketService.limit_text("\n".join(lines))

    @staticmethod
    def _format_answer_lines(form: TicketForm, answers: list[dict[str, Any]], for_admin: bool) -> list[str]:
        answer_by_question = {answer.get("question_id"): answer for answer in answers}
        lines: list[str] = []
        for question in form.questions:
            answer = answer_by_question.get(question.id)
            label = question.text.strip().rstrip(":")
            lines.append(f"{label}:")

            if answer is None:
                lines.append("— Пропущено")
            elif answer.get("skipped"):
                lines.append("пропущено")
            elif answer.get("answer_type") == "media":
                media_count = len(TicketService.extract_media_files(answer))
                if media_count:
                    lines.append(f"📎 Медиафайлов: {media_count}")
                else:
                    lines.append("— Пропущено")
            else:
                lines.append(str(answer.get("answer_text") or "не указано"))

            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
        return lines

    @staticmethod
    def extract_media_files(answer: dict[str, Any]) -> list[dict[str, Any]]:
        raw_media_files = answer.get("media_files")
        if isinstance(raw_media_files, list):
            media_files = [
                media
                for media in raw_media_files
                if isinstance(media, dict) and media.get("file_id")
            ]
            return [
                media
                for _, media in sorted(
                    enumerate(media_files),
                    key=lambda item: (TicketService._media_sort_order(item[1], item[0]), item[0]),
                )
            ]
        if answer.get("file_id"):
            return [
                {
                    "file_id": answer.get("file_id"),
                    "media_type": answer.get("media_type"),
                    "caption": answer.get("caption"),
                    "source_message_id": answer.get("source_message_id"),
                    "media_group_id": answer.get("media_group_id"),
                    "sort_order": answer.get("sort_order", answer.get("source_message_id", 0)),
                }
            ]
        return []

    @staticmethod
    def _media_sort_order(media: dict[str, Any], default: int) -> int:
        for key in ("sort_order", "source_message_id"):
            try:
                value = media.get(key)
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def get_close_reason_label(reason: str) -> str:
        close_reason = CLOSE_REASONS.get(reason)
        if close_reason is None:
            return reason
        return close_reason.label

    @staticmethod
    def build_support_close_text(reason: str, auto_close_after_days: int | None = None) -> str:
        if reason == AUTO_NO_USER_RESPONSE_REASON:
            days = auto_close_after_days or 0
            return (
                "✅ Тикет автоматически закрыт.\n"
                f"Причина: пользователь не отвечал больше {days} дней."
            )

        label = TicketService.get_close_reason_label(reason)
        return f"✅ Тикет закрыт.\nПричина: {label}."

    @staticmethod
    def build_user_close_text(reason: str, auto_close_after_days: int | None = None) -> str:
        if reason == AUTO_NO_USER_RESPONSE_REASON:
            days = auto_close_after_days or 0
            return (
                "✅ Ваш тикет был автоматически закрыт, так как в нём не было активности "
                f"больше {days} дней.\n"
                "Если вопрос ещё актуален, выберите тип обращения ниже."
            )

        label = TicketService.get_close_reason_label(reason)
        return (
            "✅ Ваш тикет был закрыт администрацией.\n"
            f"Причина: {label}.\n\n"
            "Если у вас появится новый вопрос, выберите тип обращения ниже."
        )

    async def notify_support_about_user_notification_error(self, bot: Bot, topic_id: int) -> None:
        try:
            await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=topic_id,
                text="Тикет закрыт, но уведомление пользователю отправить не удалось.",
            )
        except TelegramAPIError as notify_error:
            logger.error("Failed to notify support about close notification error topic_id=%s: %s", topic_id, notify_error)

    @staticmethod
    def limit_text(text: str) -> str:
        if len(text) <= 4096:
            return text
        return text[:4000] + "\n\n...текст обрезан, потому что Telegram ограничивает длину сообщения."
