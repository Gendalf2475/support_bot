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

        if ticket_forms:
            text = f"{text}\n\n{build_external_forms_text(ticket_forms)}"
        return await self.send_text(user, text)


def build_external_forms_text(forms: list[TicketForm]) -> str:
    lines = ["Выберите тип обращения, отправив номер или название:"]
    lines.extend(f"{index}. {form.title}" for index, form in enumerate(forms, start=1))
    return "\n".join(lines)
