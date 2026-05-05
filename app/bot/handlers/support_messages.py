from __future__ import annotations

import logging

from aiogram import Bot, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.config import Settings
from app.bot.keyboards import SUPPORT_CLOSE_TICKET_TEXT
from app.bot.services.message_service import MessageService
from app.bot.database.models import Platform
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.ticket_service import CLOSE_REASONS, MANUAL_CLOSE_REASON_CODES, TicketService
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)
router = Router(name="support_messages")


@router.message()
async def handle_support_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    if not is_support_topic_message(message, settings.support_chat_id):
        return
    if is_bot_message(message) or is_command_message(message) or not is_regular_supported_message(message):
        return

    user_service = UserService(session)
    user = await user_service.get_by_topic_id(message.message_thread_id)
    if user is None:
        logger.info("Ignored support message from unknown topic_id=%s", message.message_thread_id)
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_topic_id(message.message_thread_id)
    if ticket is None:
        logger.info("Ignored support message from topic without open ticket topic_id=%s", message.message_thread_id)
        return

    message_service = MessageService(session, settings.support_chat_id)
    delivered = None
    if user.platform == Platform.TELEGRAM.value:
        delivered = await message_service.copy_support_message_to_user(bot, message, user, ticket)
        if delivered is not None:
            await ticket_service.mark_support_activity(ticket)
        return

    if platform_router is None:
        logger.error("No platform router for support reply platform=%s user_id=%s", user.platform, user.id)
        await message.answer("Не настроен адаптер платформы пользователя.")
        return

    delivered = await platform_router.send_support_message(session, bot, message, user, ticket)
    if delivered is not None:
        await ticket_service.mark_support_activity(ticket)


@router.edited_message()
async def handle_support_edited_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    if not is_support_topic_message(message, settings.support_chat_id):
        return
    if is_bot_message(message) or is_command_message(message) or not is_regular_supported_message(message):
        return

    topic_id = message.message_thread_id
    user_service = UserService(session)
    user = await user_service.get_by_topic_id(topic_id)
    if user is None:
        logger.info("Ignored edited support message from unknown topic_id=%s", topic_id)
        return

    if user.platform != Platform.TELEGRAM.value:
        if platform_router is not None:
            text = MessageService.extract_text_or_caption(message)
            if text:
                await platform_router.send_text(user, f"✏️ Сообщение поддержки было изменено:\n\n{text}", telegram_bot=bot)
        return

    message_service = MessageService(session, settings.support_chat_id)
    await message_service.edit_support_message_copy_to_user(bot, message, user)


def is_support_topic_message(message: Message, support_chat_id: int) -> bool:
    return message.chat.id == support_chat_id and message.message_thread_id is not None


def is_bot_message(message: Message) -> bool:
    return bool(message.from_user and message.from_user.is_bot)


def is_command_message(message: Message) -> bool:
    text = MessageService.extract_text_or_caption(message)
    if not text:
        return False
    stripped_text = text.strip()
    support_system_texts = {SUPPORT_CLOSE_TICKET_TEXT}
    support_system_texts.update(CLOSE_REASONS[reason_code].button_text for reason_code in MANUAL_CLOSE_REASON_CODES)
    return stripped_text.startswith("/") or stripped_text in support_system_texts


def is_regular_supported_message(message: Message) -> bool:
    return MessageService.is_supported_message(message)
