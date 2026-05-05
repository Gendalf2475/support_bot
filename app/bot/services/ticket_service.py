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

from app.bot.database.models import Platform, Ticket, TicketAnswer, TicketAnswerMedia, TicketStatus, User, utcnow
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.ticket_formatter import TicketFormatter
from app.bot.services.ticket_form_service import (
    PROFILE_FIELD_MINECRAFT_NICKNAME,
    PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME,
    TicketCloseReason,
    TicketForm,
    TicketQuestion,
    get_minecraft_profile_field,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CloseReason:
    code: str
    label: str
    button_text: str
    user_message: str | None = None
    show_to_user: bool = True

    @property
    def id(self) -> str:
        return self.code

    @property
    def title(self) -> str:
        return self.label


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
    "force_closed_by_admin": CloseReason(
        "force_closed_by_admin",
        "Аварийно закрыт администратором",
        "Аварийно закрыт администратором",
        user_message="✅ Ваш тикет был закрыт администрацией.",
        show_to_user=True,
    ),
}
MANUAL_CLOSE_REASON_CODES = ("resolved", "no_user_response", "duplicate", "rule_violation", "other")
AUTO_NO_USER_RESPONSE_REASON = "auto_no_user_response"
FORCE_CLOSED_REASON = "force_closed_by_admin"
ACTIVE_TICKET_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.WAITING_USER)
INACTIVE_TICKET_STATUSES = (TicketStatus.CLOSED, TicketStatus.CANCELLED)
SUPPORT_WAITING_STATUSES = (TicketStatus.OPEN, TicketStatus.IN_PROGRESS)
DEFAULT_TICKET_SUCCESS_TEXT = "✅ Тикет отправлен в поддержку.\nОтвет придёт сюда."
USER_REPLY_REMINDER_TEXT = "⏳ Поддержка ожидает ваш ответ.\n\nПожалуйста, напишите сообщение сюда."
AUTO_CLOSE_WARNING_TEXT = "⏰ Ваш тикет будет закрыт через {hours} часа, если вы не ответите."


class SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass(frozen=True)
class CloseTicketResult:
    ticket: Ticket
    closed: bool
    already_closed: bool
    reason_id: str
    reason_title: str

    def __bool__(self) -> bool:
        return self.closed


class TicketService:
    def __init__(self, session: AsyncSession, support_chat_id: int) -> None:
        self.session = session
        self.support_chat_id = support_chat_id

    async def get_open_ticket_by_user_id(self, user_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.user_id == user_id, Ticket.status.in_(ACTIVE_TICKET_STATUSES))
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        ticket = await self.session.scalar(statement)
        await self._log_active_ticket_lookup(user_id=user_id, ticket=ticket)
        return ticket

    async def get_open_ticket_by_topic_id(self, topic_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.topic_id == topic_id, Ticket.status.in_(ACTIVE_TICKET_STATUSES))
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        ticket = await self.session.scalar(statement)
        if ticket is not None:
            logger.info(
                "Active ticket lookup by topic topic_id=%s found ticket_id=%s ticket_status=%s",
                topic_id,
                ticket.id,
                ticket.status.value,
            )
        return ticket

    async def get_latest_ticket_by_topic_id(self, topic_id: int) -> Ticket | None:
        statement = (
            select(Ticket)
            .where(Ticket.topic_id == topic_id)
            .options(selectinload(Ticket.answers).selectinload(TicketAnswer.media_files))
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        return await self.session.scalar(statement)

    async def _log_active_ticket_lookup(self, *, user_id: int, ticket: Ticket | None) -> None:
        user = await self.session.get(User, user_id)
        if ticket is not None:
            logger.info(
                "Active ticket lookup user_id=%s platform=%s platform_user_id=%s found ticket_id=%s ticket_status=%s",
                user_id,
                user.platform if user else None,
                user.platform_user_id if user else None,
                ticket.id,
                ticket.status.value,
            )
            return

        latest_statement = (
            select(Ticket)
            .where(Ticket.user_id == user_id)
            .order_by(Ticket.created_at.desc())
            .limit(1)
        )
        latest_ticket = await self.session.scalar(latest_statement)
        if latest_ticket is not None and latest_ticket.status in INACTIVE_TICKET_STATUSES:
            logger.info(
                "Ignored inactive ticket id=%s status=%s user_id=%s platform=%s platform_user_id=%s",
                latest_ticket.id,
                latest_ticket.status.value,
                user_id,
                user.platform if user else None,
                user.platform_user_id if user else None,
            )
            return

        logger.info(
            "Active ticket lookup user_id=%s platform=%s platform_user_id=%s found no active ticket",
            user_id,
            user.platform if user else None,
            user.platform_user_id if user else None,
        )

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
            platform=user.platform,
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
        logger.info(
            "Created open ticket id=%s platform=%s platform_user_id=%s topic_id=%s",
            ticket.id,
            user.platform,
            user.platform_user_id,
            topic_id,
        )
        return ticket

    async def create_cancelled_ticket(
        self,
        user: User,
        form: TicketForm,
        answers: list[dict[str, Any]],
    ) -> Ticket:
        ticket = Ticket(
            user_id=user.id,
            platform=user.platform,
            form_id=form.id,
            form_title=form.admin_title,
            status=TicketStatus.CANCELLED,
            topic_id=user.topic_id,
        )
        self.session.add(ticket)
        await self.session.flush()
        self._add_answers(ticket, form, answers)
        await self.session.flush()
        logger.info("Created cancelled ticket id=%s platform=%s platform_user_id=%s", ticket.id, user.platform, user.platform_user_id)
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
        ticket.last_user_reply_reminded_at = None
        ticket.auto_close_warning_sent_at = None
        if ticket.status == TicketStatus.WAITING_USER:
            ticket.status = TicketStatus.IN_PROGRESS
        elif ticket.status == TicketStatus.OPEN:
            ticket.status = TicketStatus.IN_PROGRESS
        await self.session.flush()

    async def mark_support_activity(self, ticket: Ticket) -> None:
        if ticket.status not in {TicketStatus.CLOSED, TicketStatus.CANCELLED}:
            ticket.status = TicketStatus.WAITING_USER
            ticket.last_support_message_at = utcnow()
            ticket.last_user_reply_reminded_at = None
        await self.session.flush()

    async def close_ticket(
        self,
        bot: Bot,
        ticket: Ticket,
        reason: str,
        closed_by_telegram_id: int | None = None,
        auto_close_after_days: int | None = None,
        ticket_forms: list[TicketForm] | None = None,
        platform_router: PlatformRouter | None = None,
    ) -> CloseTicketResult:
        fresh_ticket = await self.get_ticket_by_id(ticket.id)
        if fresh_ticket is None:
            raise ValueError(f"Ticket not found: {ticket.id}")

        logger.info("Closing ticket id=%s current_status=%s", fresh_ticket.id, fresh_ticket.status.value)
        if fresh_ticket.status in INACTIVE_TICKET_STATUSES:
            reason_title = self.get_close_reason_label(fresh_ticket.close_reason or reason)
            logger.info("Ticket already closed id=%s status=%s", fresh_ticket.id, fresh_ticket.status.value)
            return CloseTicketResult(
                ticket=fresh_ticket,
                closed=False,
                already_closed=True,
                reason_id=fresh_ticket.close_reason or reason,
                reason_title=reason_title,
            )

        close_reason = self.resolve_close_reason(fresh_ticket, reason, ticket_forms)
        if close_reason is None:
            raise ValueError(f"Unknown ticket close reason: {reason}")

        now = utcnow()
        fresh_ticket.status = TicketStatus.CLOSED
        fresh_ticket.close_reason = reason
        fresh_ticket.closed_at = now
        fresh_ticket.closed_by_telegram_id = closed_by_telegram_id
        fresh_ticket.updated_at = now
        await self.session.flush()
        await self.session.commit()

        user = await self.session.get(User, fresh_ticket.user_id)
        topic_id = fresh_ticket.topic_id or (user.topic_id if user else None)
        topic_text = self.build_support_close_text(reason, auto_close_after_days, close_reason)
        if topic_id:
            try:
                await bot.send_message(
                    chat_id=self.support_chat_id,
                    message_thread_id=topic_id,
                    text=topic_text,
                    reply_markup=ReplyKeyboardRemove(),
                )
            except TelegramAPIError as error:
                logger.error("Failed to send ticket closed notice ticket_id=%s topic_id=%s: %s", fresh_ticket.id, topic_id, error)

        if user:
            if platform_router is not None:
                platform_router.clear_user_state(user.platform, user.platform_user_id)
            notification_sent = False
            if platform_router is not None:
                sent = await platform_router.send_ticket_closed(
                    user=user,
                    text=self.build_user_close_text(reason, auto_close_after_days, close_reason),
                    telegram_bot=bot,
                    ticket_forms=ticket_forms,
                )
                notification_sent = sent is not None or user.blocked
            elif user.platform == Platform.TELEGRAM.value and user.telegram_id is not None:
                try:
                    reply_markup = None
                    if not user.blocked and ticket_forms:
                        from app.bot.keyboards import ticket_forms_reply_keyboard

                        reply_markup = ticket_forms_reply_keyboard(ticket_forms)

                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=self.build_user_close_text(reason, auto_close_after_days, close_reason),
                        reply_markup=reply_markup,
                    )
                    notification_sent = True
                except TelegramAPIError as error:
                    logger.error("Failed to notify user about closed ticket ticket_id=%s telegram_id=%s: %s", fresh_ticket.id, user.telegram_id, error)

            if not notification_sent and topic_id:
                await self.notify_support_about_user_notification_error(bot, topic_id)

        if user:
            await self.update_ticket_card(bot, fresh_ticket, user, get_close_reason_title(close_reason))
        await self.update_ticket_control_message(bot, fresh_ticket)

        logger.info(
            "Ticket closed id=%s reason_id=%s reason_title=%s by telegram_id=%s",
            fresh_ticket.id,
            reason,
            get_close_reason_title(close_reason),
            closed_by_telegram_id,
        )
        return CloseTicketResult(
            ticket=fresh_ticket,
            closed=True,
            already_closed=False,
            reason_id=reason,
            reason_title=get_close_reason_title(close_reason),
        )

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
            Ticket.status.in_(SUPPORT_WAITING_STATUSES),
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
        platform_router: PlatformRouter | None = None,
    ) -> int:
        now = utcnow()
        inactive_before = now - timedelta(days=auto_close_after_days)
        statement = (
            select(Ticket)
            .where(
                Ticket.status.in_(ACTIVE_TICKET_STATUSES),
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
                    platform_router=platform_router,
                )
            except Exception as error:
                logger.exception("Failed to auto-close ticket_id=%s: %s", ticket.id, error)
                continue
            if closed:
                closed_count += 1

        return closed_count

    async def send_auto_close_warnings(
        self,
        bot: Bot,
        auto_close_after_days: int,
        warning_hours: int,
        platform_router: PlatformRouter | None = None,
    ) -> int:
        now = utcnow()
        warning_delta = timedelta(hours=max(0, warning_hours))
        auto_close_delta = timedelta(days=auto_close_after_days)
        statement = (
            select(Ticket)
            .where(
                Ticket.status.in_(ACTIVE_TICKET_STATUSES),
                Ticket.auto_close_warning_sent_at.is_(None),
            )
            .options(selectinload(Ticket.user))
        )
        tickets = list((await self.session.scalars(statement)).all())
        sent_count = 0
        for ticket in tickets:
            user = ticket.user
            if user is None or user.blocked:
                continue
            last_activity_at = ticket.last_user_message_at or ticket.created_at
            auto_close_at = last_activity_at + auto_close_delta
            warning_at = auto_close_at - warning_delta
            if now < warning_at or now >= auto_close_at:
                continue
            text = AUTO_CLOSE_WARNING_TEXT.format(hours=warning_hours)
            try:
                sent = await self.send_user_maintenance_text(bot, ticket, user, text, platform_router)
                if sent:
                    sent_count += 1
            except Exception as error:
                logger.exception("Failed to send auto-close warning ticket_id=%s: %s", ticket.id, error)
                await self.notify_support_about_user_notification_error(
                    bot,
                    ticket.topic_id,
                    "Не удалось отправить пользователю предупреждение перед автозакрытием.",
                )
            ticket.auto_close_warning_sent_at = now

        await self.session.flush()
        return sent_count

    async def send_waiting_user_reminders(
        self,
        bot: Bot,
        reminder_after_hours: int,
        reminder_interval_hours: int,
        platform_router: PlatformRouter | None = None,
    ) -> int:
        now = utcnow()
        first_reminder_before = now - timedelta(hours=reminder_after_hours)
        repeat_before = now - timedelta(hours=reminder_interval_hours) if reminder_interval_hours > 0 else None
        statement = (
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.WAITING_USER,
                or_(
                    Ticket.last_support_message_at <= first_reminder_before,
                    and_(Ticket.last_support_message_at.is_(None), Ticket.updated_at <= first_reminder_before),
                ),
            )
            .options(selectinload(Ticket.user))
        )
        tickets = list((await self.session.scalars(statement)).all())
        sent_count = 0
        for ticket in tickets:
            user = ticket.user
            if user is None or user.blocked:
                continue
            if ticket.last_user_reply_reminded_at is not None:
                if repeat_before is None or ticket.last_user_reply_reminded_at > repeat_before:
                    continue
            try:
                sent = await self.send_user_maintenance_text(bot, ticket, user, USER_REPLY_REMINDER_TEXT, platform_router)
                if sent:
                    sent_count += 1
            except Exception as error:
                logger.exception("Failed to send waiting-user reminder ticket_id=%s: %s", ticket.id, error)
                await self.notify_support_about_user_notification_error(
                    bot,
                    ticket.topic_id,
                    "Не удалось отправить пользователю напоминание об ожидаемом ответе.",
                )
            ticket.last_user_reply_reminded_at = now

        await self.session.flush()
        return sent_count

    async def send_user_maintenance_text(
        self,
        bot: Bot,
        ticket: Ticket,
        user: User,
        text: str,
        platform_router: PlatformRouter | None = None,
    ) -> bool:
        sent = None
        if platform_router is not None:
            sent = await platform_router.send_text(user, text, telegram_bot=bot)
        elif user.platform == Platform.TELEGRAM.value and user.telegram_id is not None:
            try:
                await bot.send_message(chat_id=user.telegram_id, text=text)
                return True
            except TelegramAPIError as error:
                logger.error("Failed to send Telegram maintenance message ticket_id=%s telegram_id=%s: %s", ticket.id, user.telegram_id, error)
        if sent is not None:
            return True
        logger.error("Failed to send maintenance message ticket_id=%s platform=%s platform_user_id=%s", ticket.id, user.platform, user.platform_user_id)
        await self.notify_support_about_user_notification_error(
            bot,
            ticket.topic_id,
            "Не удалось отправить пользователю служебное уведомление.",
        )
        return False

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
                minecraft_lookup = answer.get("minecraft_lookup") if isinstance(answer.get("minecraft_lookup"), dict) else {}
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
                    profile_field=answer.get("profile_field") or get_minecraft_profile_field(question),
                    minecraft_lookup_nickname=minecraft_lookup.get("nickname"),
                    minecraft_lookup_exists=minecraft_lookup.get("exists"),
                    minecraft_lookup_uuid=minecraft_lookup.get("uuid"),
                    minecraft_lookup_online=minecraft_lookup.get("online"),
                    minecraft_lookup_source=minecraft_lookup.get("source"),
                    minecraft_lookup_error=minecraft_lookup.get("error"),
                )
                for media_index, media in enumerate(media_files):
                    ticket_answer.media_files.append(
                        TicketAnswerMedia(
                            file_id=str(media.get("file_id") or ""),
                            media_type=str(media.get("media_type") or "media"),
                            caption=media.get("caption"),
                            file_url=media.get("file_url"),
                            filename=media.get("filename"),
                            mime_type=media.get("mime_type"),
                            media_group_id=media.get("media_group_id"),
                            sort_order=TicketService._media_sort_order(media, media_index),
                        )
                    )
                self.session.add(ticket_answer)

    async def update_ticket_card(self, bot: Bot, ticket: Ticket, user: User, close_reason_label: str | None = None) -> None:
        if not ticket.card_message_id:
            return

        if close_reason_label is None and ticket.close_reason:
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
        logger.debug("building user summary form_id=%s answers_count=%s", form.id, len(answers))
        lines = [
            "Проверьте данные тикета:",
            "",
            f"Тип обращения: {form.title}",
            "",
        ]
        lines.extend(TicketService._format_answer_lines(form, answers, for_admin=False))
        lines.extend(["", "Отправить тикет?"])
        text = "\n".join(lines)
        limited_text = TicketService.limit_text(text, limit=4000)
        logger.debug(
            "building user summary form_id=%s summary length=%s summary truncated=%s",
            form.id,
            len(text),
            limited_text != text,
        )
        return limited_text

    @staticmethod
    def build_admin_ticket_text(form: TicketForm, user: User, answers: list[dict[str, Any]]) -> str:
        if user.username and user.platform == Platform.TELEGRAM.value:
            username = f"@{str(user.username).strip('@')}"
        else:
            username = str(user.username).strip() if user.username else "нет username"
        full_name = user.full_name or "не указано"
        lines = [
            "🟣 Новый тикет",
            "",
            f"Платформа: {user.platform}",
            f"Тип: {form.admin_title}",
            f"Username: {username}",
            f"Platform ID: {user.platform_user_id}",
            f"Имя: {full_name}",
            "",
            "Данные формы:",
            "",
        ]
        lines.extend(TicketService._format_answer_lines(form, answers, for_admin=True))
        return TicketService.limit_text("\n".join(lines), limit=4000)

    @staticmethod
    def _format_answer_lines(form: TicketForm, answers: list[dict[str, Any]], for_admin: bool) -> list[str]:
        answer_by_question = {answer.get("question_id"): answer for answer in answers}
        lines: list[str] = []
        for question in form.questions:
            answer = answer_by_question.get(question.id)
            label = TicketService.get_preview_question_label(question)
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
    def get_preview_question_label(question: TicketQuestion) -> str:
        profile_field = get_minecraft_profile_field(question)
        if profile_field == PROFILE_FIELD_MINECRAFT_NICKNAME:
            return "Игровой ник"
        if profile_field == PROFILE_FIELD_MINECRAFT_TARGET_NICKNAME:
            return "Ник нарушителя"
        return question.text.strip().rstrip(":")

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
                    "file_url": answer.get("file_url"),
                    "filename": answer.get("filename"),
                    "mime_type": answer.get("mime_type"),
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
        return get_close_reason_title(close_reason)

    @staticmethod
    def fallback_close_reasons() -> list[CloseReason]:
        return [CLOSE_REASONS[reason_code] for reason_code in MANUAL_CLOSE_REASON_CODES]

    @staticmethod
    def get_form_close_reasons(ticket: Ticket, ticket_forms: list[TicketForm] | None) -> list[TicketCloseReason | CloseReason]:
        form = TicketService.get_ticket_form(ticket, ticket_forms)
        if form is not None and form.close_reasons:
            return list(form.close_reasons)
        return TicketService.fallback_close_reasons()

    @staticmethod
    def get_ticket_form(ticket: Ticket, ticket_forms: list[TicketForm] | None) -> TicketForm | None:
        if not ticket_forms:
            return None
        return next((form for form in ticket_forms if form.id == ticket.form_id), None)

    @staticmethod
    def resolve_close_reason(
        ticket: Ticket,
        reason: str,
        ticket_forms: list[TicketForm] | None = None,
    ) -> TicketCloseReason | CloseReason | None:
        if reason in {AUTO_NO_USER_RESPONSE_REASON, FORCE_CLOSED_REASON}:
            return CLOSE_REASONS[reason]
        return next((close_reason for close_reason in TicketService.get_form_close_reasons(ticket, ticket_forms) if close_reason.id == reason), None)

    @staticmethod
    def build_support_close_text(
        reason: str,
        auto_close_after_days: int | None = None,
        close_reason: TicketCloseReason | CloseReason | None = None,
    ) -> str:
        if reason == AUTO_NO_USER_RESPONSE_REASON:
            days = auto_close_after_days or 0
            return (
                "✅ Тикет автоматически закрыт.\n"
                f"Причина: пользователь не отвечал больше {days} дней."
            )

        label = get_close_reason_title(close_reason) if close_reason is not None else TicketService.get_close_reason_label(reason)
        return f"✅ Тикет закрыт.\nПричина: {label}."

    @staticmethod
    def build_user_close_text(
        reason: str,
        auto_close_after_days: int | None = None,
        close_reason: TicketCloseReason | CloseReason | None = None,
    ) -> str:
        if reason == AUTO_NO_USER_RESPONSE_REASON:
            days = auto_close_after_days or 0
            return TicketService.limit_text(
                (
                    "✅ Ваш тикет был автоматически закрыт, так как в нём не было активности "
                    f"больше {days} дней.\n"
                    "Если вопрос ещё актуален, выберите тип обращения ниже."
                ),
                limit=4000,
            )

        if close_reason is not None and close_reason.show_to_user and close_reason.user_message:
            return TicketService.limit_text(close_reason.user_message, limit=4000)

        label = get_close_reason_title(close_reason) if close_reason is not None else TicketService.get_close_reason_label(reason)
        return TicketService.limit_text(
            "✅ Ваш тикет был закрыт администрацией.\n"
            f"Причина: {label}.\n\n"
            "Если у вас появится новый вопрос, выберите тип обращения ниже.",
            limit=4000,
        )

    @staticmethod
    def build_success_text(form: TicketForm, ticket: Ticket, user: User) -> str:
        template = form.success_text or DEFAULT_TICKET_SUCCESS_TEXT
        values = SafeFormatDict(
            {
                "ticket_id": ticket.id,
                "form_title": form.title,
                "platform": user.platform,
            }
        )
        try:
            return TicketService.limit_text(template.format_map(values), limit=4000)
        except Exception as error:
            logger.error("Failed to render success_text form_id=%s ticket_id=%s: %s", form.id, ticket.id, error)
            return TicketService.limit_text(template, limit=4000)

    @staticmethod
    def limit_text(text: str | None, limit: int = 4000) -> str:
        if text is None:
            return ""
        normalized = str(text)
        if limit <= 0:
            return ""
        if len(normalized) <= limit:
            return normalized
        suffix = "\n\n…"
        if limit <= len(suffix):
            return normalized[:limit]
        return normalized[: limit - len(suffix)].rstrip() + suffix

    async def notify_support_about_user_notification_error(
        self,
        bot: Bot,
        topic_id: int | None,
        text: str = "Тикет закрыт, но уведомление пользователю отправить не удалось.",
    ) -> None:
        if topic_id is None:
            return
        try:
            await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=topic_id,
                text=text,
            )
        except TelegramAPIError as notify_error:
            logger.error("Failed to notify support about close notification error topic_id=%s: %s", topic_id, notify_error)


def get_close_reason_title(reason: TicketCloseReason | CloseReason) -> str:
    return reason.title or reason.button_text or reason.id
