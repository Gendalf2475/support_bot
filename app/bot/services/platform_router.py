from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.channels.base import ChannelAdapter, OutgoingMessage, SentMessageRef
from app.bot.database.models import MessageDirection, Platform, Ticket, User
from app.bot.services.message_service import MessageService
from app.bot.services.ticket_form_service import TicketForm


logger = logging.getLogger(__name__)


class PlatformRouter:
    def __init__(self) -> None:
        self.adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        self.adapters[adapter.platform] = adapter

    async def send_text(
        self,
        user: User,
        text: str,
        *,
        telegram_bot: Bot | None = None,
        telegram_reply_markup: Any | None = None,
    ) -> SentMessageRef | None:
        if user.platform == Platform.TELEGRAM.value:
            if telegram_bot is None or user.telegram_id is None:
                logger.error("Telegram delivery requested without bot or telegram_id user_id=%s", user.id)
                return None
            try:
                sent = await telegram_bot.send_message(
                    chat_id=user.telegram_id,
                    text=text,
                    reply_markup=telegram_reply_markup,
                )
                return SentMessageRef(platform_message_id=str(sent.message_id))
            except TelegramAPIError as error:
                logger.error("Failed to send Telegram message user_id=%s telegram_id=%s: %s", user.id, user.telegram_id, error)
                return None

        adapter = self.adapters.get(user.platform)
        if adapter is None:
            logger.error("No channel adapter registered for platform=%s user_id=%s", user.platform, user.id)
            return None

        try:
            return await adapter.send_message(
                OutgoingMessage(
                    platform=user.platform,
                    platform_user_id=user.platform_user_id,
                    text=text,
                )
            )
        except Exception as error:
            logger.exception("Failed to send platform message platform=%s user_id=%s: %s", user.platform, user.id, error)
            return None

    async def send_form_menu(
        self,
        user: User,
        forms: list[TicketForm],
        *,
        text: str | None = None,
        telegram_bot: Bot | None = None,
    ) -> SentMessageRef | None:
        if user.platform == Platform.TELEGRAM.value:
            from app.bot.keyboards import ticket_forms_reply_keyboard

            reply_markup = ticket_forms_reply_keyboard(forms) if forms else None
            return await self.send_text(user, text or build_external_start_text(), telegram_bot=telegram_bot, telegram_reply_markup=reply_markup)

        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_form_menu", None) if adapter is not None else None
        if callable(sender):
            try:
                sent = await sender(user, forms, text)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform form menu platform=%s user_id=%s: %s", user.platform, user.id, error)

        fallback_text = build_external_start_text()
        if text:
            fallback_text = f"{text.strip()}\n\n{fallback_text}"
        if forms:
            fallback_text = f"{fallback_text}\n\n{build_external_forms_text(forms)}"
        return await self.send_text(user, fallback_text, telegram_bot=telegram_bot)

    async def send_question(
        self,
        user: User,
        form: TicketForm,
        question_index: int,
        *,
        prefix_text: str | None = None,
        telegram_bot: Bot | None = None,
    ) -> SentMessageRef | None:
        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_question", None) if adapter is not None else None
        if callable(sender):
            try:
                sent = await sender(user, form, question_index, prefix_text)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform question platform=%s user_id=%s: %s", user.platform, user.id, error)
        return await self.send_text(user, build_external_question_text(form, question_index, prefix_text), telegram_bot=telegram_bot)

    async def send_media_continue(
        self,
        user: User,
        question_index: int,
        media_count: int,
        max_files: int | None,
        *,
        limit_reached: bool = False,
        telegram_bot: Bot | None = None,
    ) -> SentMessageRef | None:
        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_media_continue", None) if adapter is not None else None
        if callable(sender):
            try:
                sent = await sender(user, question_index, media_count, max_files, limit_reached=limit_reached)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform media controls platform=%s user_id=%s: %s", user.platform, user.id, error)
        return await self.send_text(user, build_external_media_continue_text(media_count, max_files, limit_reached), telegram_bot=telegram_bot)

    async def send_ticket_preview(
        self,
        user: User,
        form: TicketForm,
        answers: list[dict[str, Any]],
        *,
        telegram_bot: Bot | None = None,
    ) -> SentMessageRef | None:
        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_ticket_preview", None) if adapter is not None else None
        if callable(sender):
            try:
                sent = await sender(user, form, answers)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform ticket preview platform=%s user_id=%s: %s", user.platform, user.id, error)
        text = f"{build_external_preview_text(form, answers)}\n\nНапишите: Отправить, Заново или Отмена."
        return await self.send_text(user, text, telegram_bot=telegram_bot)

    async def send_ticket_sent(
        self,
        user: User,
        ticket_id: int | None = None,
        success_text: str | None = None,
        *,
        telegram_bot: Bot | None = None,
    ) -> SentMessageRef | None:
        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_ticket_sent", None) if adapter is not None else None
        if callable(sender):
            try:
                sent = await sender(user, ticket_id, success_text)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform ticket sent notice platform=%s user_id=%s: %s", user.platform, user.id, error)
        text = success_text or "✅ Тикет отправлен в поддержку.\nОтвет придёт сюда."
        if ticket_id is not None and success_text is None:
            text = f"{text}\nТикет: #{ticket_id}"
        return await self.send_text(user, text, telegram_bot=telegram_bot)

    async def send_support_message(
        self,
        session: AsyncSession,
        telegram_bot: Bot,
        support_message: Message,
        user: User,
        ticket: Ticket | None = None,
    ) -> int | None:
        if user.platform == Platform.TELEGRAM.value:
            return await MessageService(session, support_message.chat.id).copy_support_message_to_user(
                telegram_bot,
                support_message,
                user,
                ticket,
            )

        text = MessageService.extract_text_or_caption(support_message)
        has_media = any(
            getattr(support_message, field, None)
            for field in ("photo", "video", "document", "voice", "audio", "sticker", "animation")
        )
        if text and has_media:
            outgoing_text = (
                f"{text}\n\n"
                "Администрация отправила медиафайл, но его не удалось переслать в эту платформу."
            )
        elif text:
            outgoing_text = text
        else:
            outgoing_text = "Администрация отправила медиафайл, но его не удалось переслать в эту платформу."

        sent = await self.send_text(user, outgoing_text)
        platform_message_id = sent.platform_message_id if sent else None
        if support_message.message_thread_id is not None:
            await MessageService(session, support_message.chat.id).create_message_map(
                user=user,
                user_message_id=None,
                support_message_id=support_message.message_id,
                topic_id=support_message.message_thread_id,
                direction=MessageDirection.SUPPORT_TO_USER,
                ticket_id=ticket.id if ticket else None,
                platform=user.platform,
                platform_message_id=platform_message_id,
                telegram_support_message_id=support_message.message_id,
            )
        return int(platform_message_id) if platform_message_id and platform_message_id.isdigit() else None

    async def send_ticket_closed(
        self,
        user: User,
        text: str,
        *,
        telegram_bot: Bot,
        ticket_forms: list[TicketForm] | None = None,
    ) -> SentMessageRef | None:
        if user.blocked:
            return None

        if user.platform == Platform.TELEGRAM.value:
            from app.bot.keyboards import ticket_forms_reply_keyboard

            reply_markup = ticket_forms_reply_keyboard(ticket_forms) if ticket_forms else None
            return await self.send_text(user, text, telegram_bot=telegram_bot, telegram_reply_markup=reply_markup)

        adapter = self.adapters.get(user.platform)
        sender = getattr(adapter, "send_closed_ticket_menu", None) if adapter is not None else None
        if ticket_forms and callable(sender):
            try:
                sent = await sender(user, text, ticket_forms)
                if sent is not None:
                    return sent
            except Exception as error:
                logger.exception("Failed to send platform ticket closed menu platform=%s user_id=%s: %s", user.platform, user.id, error)

        if ticket_forms:
            text = f"{text}\n\n{build_external_forms_text(ticket_forms)}"
        return await self.send_text(user, text)


def build_external_start_text() -> str:
    return (
        "Здравствуйте! Здесь вы можете обратиться в поддержку.\n\n"
        "Выберите тип обращения ниже."
    )


def build_external_forms_text(forms: list[TicketForm]) -> str:
    lines = ["Выберите тип обращения, отправив номер или название:"]
    lines.extend(f"{index}. {form.title}" for index, form in enumerate(forms, start=1))
    return "\n".join(lines)


def build_external_question_text(form: TicketForm, question_index: int, prefix_text: str | None = None) -> str:
    question = form.questions[question_index]
    lines = [
        form.title,
        "",
        f"Вопрос {question_index + 1} из {len(form.questions)}",
        "",
        question.text,
    ]
    if question.help_text:
        lines.extend(["", f"Подсказка: {question.help_text}"])
    if prefix_text:
        lines = [prefix_text.strip(), ""] + lines
    return "\n".join(lines)


def build_external_media_continue_text(media_count: int, max_files: int | None, limit_reached: bool = False) -> str:
    if max_files is None:
        return (
            f"Файлы добавлены: {media_count}.\n"
            "Можно отправить ещё файл или написать «Продолжить»."
        )
    if limit_reached:
        return (
            f"Файлы добавлены: {media_count} из {max_files}.\n"
            "Достигнут лимит файлов.\n\n"
            "Напишите «Продолжить», чтобы перейти дальше."
        )
    return (
        f"Файлы добавлены: {media_count} из {max_files}.\n"
        "Можно отправить ещё файл или написать «Продолжить»."
    )


def build_external_preview_text(form: TicketForm, answers: list[dict[str, Any]]) -> str:
    lines = [
        "Проверьте данные тикета:",
        "",
        f"Тип обращения: {form.title}",
        "",
    ]
    answers_by_question = {answer.get("question_id"): answer for answer in answers}
    for question in form.questions:
        answer = answers_by_question.get(question.id)
        lines.append(f"{question.text.strip().rstrip(':')}:")
        lines.append(format_external_preview_answer(answer))
        lines.append("")
    lines.append("Отправить тикет?")
    return "\n".join(lines)


def format_external_preview_answer(answer: dict[str, Any] | None) -> str:
    if answer is None or answer.get("skipped"):
        return "— Пропущено"
    if answer.get("answer_type") == "media":
        media_files = answer.get("media_files")
        if isinstance(media_files, list):
            media_count = len([media for media in media_files if isinstance(media, dict) and media.get("file_id")])
        else:
            media_count = 1 if answer.get("file_id") else 0
        return f"📎 Медиафайлов: {media_count}" if media_count else "— Пропущено"
    return str(answer.get("answer_text") or "не указано")
