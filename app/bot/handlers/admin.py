from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.config import Settings
from app.bot.database.models import Ticket, TicketStatus, User
from app.bot.keyboards import (
    CALLBACK_CLOSE_PREFIX,
    CALLBACK_CLOSE_REASON_PREFIX,
    close_reason_keyboard,
    closed_ticket_keyboard,
    SUPPORT_CLOSE_TICKET_TEXT,
    support_close_reason_keyboard,
)
from app.bot.services.ticket_form_service import TicketFormService
from app.bot.services.ticket_service import CLOSE_REASONS, MANUAL_CLOSE_REASON_CODES, TicketService
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)
router = Router(name="admin")


@router.message(Command("block"))
async def block_user(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    user = await get_command_user(message, session, settings)
    if user is None:
        return

    await UserService(session).set_blocked(user, True)
    logger.info("Blocked user platform=%s platform_user_id=%s topic_id=%s", user.platform, user.platform_user_id, user.topic_id)
    await send_to_topic(bot, settings.support_chat_id, user.topic_id, "Пользователь заблокирован.")
    await notify_user(
        bot,
        settings.support_chat_id,
        user,
        "Вы были заблокированы службой поддержки.",
        "Пользователь заблокирован, но уведомление в ЛС отправить не удалось.",
        platform_router=platform_router,
    )


@router.message(Command("unblock"))
async def unblock_user(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    user = await get_command_user(message, session, settings)
    if user is None:
        return

    await UserService(session).set_blocked(user, False)
    logger.info("Unblocked user platform=%s platform_user_id=%s topic_id=%s", user.platform, user.platform_user_id, user.topic_id)
    await send_to_topic(bot, settings.support_chat_id, user.topic_id, "Пользователь разблокирован.")
    await notify_user(
        bot,
        settings.support_chat_id,
        user,
        "Вы были разблокированы службой поддержки. Теперь вы снова можете писать сюда.",
        "Пользователь разблокирован, но уведомление в ЛС отправить не удалось.",
        platform_router=platform_router,
    )


@router.message(Command("status"))
async def user_status(message: Message, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    user = await get_command_user(message, session, settings)
    if user is None:
        return

    open_ticket = await TicketService(session, settings.support_chat_id).get_open_ticket_by_user_id(user.id)
    text = (
        "🟣 Статус пользователя\n\n"
        f"user_id: {user.id}\n"
        f"Платформа: {format_platform(user)}\n"
        f"Platform ID: {user.platform_user_id}\n"
        f"Username: {format_username(user)}\n"
        f"Имя: {user.full_name or 'не указано'}\n"
        f"Topic ID: {user.topic_id}\n"
        f"Blocked: {user.blocked}\n"
        f"Открытый тикет: #{open_ticket.id if open_ticket else 'нет'}\n"
        f"Статус тикета: {open_ticket.status.value if open_ticket else 'нет'}\n"
        f"ticket_created_at: {format_dt(open_ticket.created_at) if open_ticket else 'нет'}\n"
        f"created_at: {format_dt(user.created_at)}\n"
        f"updated_at: {format_dt(user.updated_at)}"
    )
    await send_to_topic(bot, settings.support_chat_id, user.topic_id, text)


@router.message(Command("close"))
async def close_ticket_by_command(message: Message, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    if message.message_thread_id is None:
        await message.answer("Команда /close работает только внутри топика тикета.")
        return
    if message.from_user is None:
        await message.answer("Не удалось определить, кто закрывает тикет.")
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_topic_id(message.message_thread_id)
    if ticket is None:
        await message.answer("В этом топике нет открытого тикета.")
        return

    await prompt_close_reason(message, ticket)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text == SUPPORT_CLOSE_TICKET_TEXT)
async def close_ticket_by_keyboard(message: Message, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    if message.message_thread_id is None:
        await message.answer("Кнопка закрытия работает только внутри топика тикета.")
        return
    if message.from_user is None:
        await message.answer("Не удалось определить, кто закрывает тикет.")
        return
    if not await is_support_chat_member(bot, settings.support_chat_id, message.from_user.id):
        await message.answer("Закрывать тикеты могут только участники группы поддержки.")
        return

    ticket = await TicketService(session, settings.support_chat_id).get_open_ticket_by_topic_id(message.message_thread_id)
    if ticket is None:
        await message.answer("Нет открытого тикета для закрытия.", reply_markup=ReplyKeyboardRemove())
        return

    await prompt_close_reason(message, ticket)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text.in_([CLOSE_REASONS[reason_code].button_text for reason_code in MANUAL_CLOSE_REASON_CODES]),
)
async def close_ticket_by_reason_keyboard(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
    platform_router: PlatformRouter | None = None,
) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    if message.message_thread_id is None:
        await message.answer("Причину закрытия можно выбрать только внутри топика тикета.")
        return
    if message.from_user is None:
        await message.answer("Не удалось определить, кто закрывает тикет.")
        return
    if not await is_support_chat_member(bot, settings.support_chat_id, message.from_user.id):
        await message.answer("Закрывать тикеты могут только участники группы поддержки.")
        return

    reason = get_close_reason_code_by_button_text(message.text or "")
    if reason is None:
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_topic_id(message.message_thread_id)
    if ticket is None:
        await message.answer("Нет открытого тикета для закрытия.", reply_markup=ReplyKeyboardRemove())
        return

    closed = await close_ticket(
        bot=bot,
        ticket_service=ticket_service,
        ticket=ticket,
        closed_by_telegram_id=message.from_user.id,
        reason=reason,
        ticket_form_service=ticket_form_service,
        platform_router=platform_router,
    )
    if not closed:
        await message.answer("Тикет уже закрыт.")


@router.callback_query(F.data.startswith(CALLBACK_CLOSE_PREFIX))
async def close_ticket_by_callback(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Сообщение с кнопкой недоступно.", show_alert=True)
        return
    if message.chat.id != settings.support_chat_id:
        await callback.answer("Кнопка работает только в группе поддержки.", show_alert=True)
        return
    if message.message_thread_id is None:
        await callback.answer("Кнопка работает только внутри топика тикета.", show_alert=True)
        return
    if not await is_support_chat_member(bot, settings.support_chat_id, callback.from_user.id):
        await callback.answer("Закрывать тикеты могут только участники группы поддержки.", show_alert=True)
        return

    ticket_id = parse_ticket_id(callback.data)
    if ticket_id is None:
        await callback.answer("Некорректная кнопка закрытия тикета.", show_alert=True)
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    error = validate_ticket_for_callback(ticket, message.message_thread_id)
    if error:
        await callback.answer(error, show_alert=True)
        return

    assert ticket is not None
    try:
        await bot.edit_message_reply_markup(
            chat_id=settings.support_chat_id,
            message_id=message.message_id,
            reply_markup=close_reason_keyboard(ticket.id),
        )
    except TelegramAPIError as error:
        logger.error("Failed to show close reasons ticket_id=%s message_id=%s: %s", ticket.id, message.message_id, error)
        await bot.send_message(
            chat_id=settings.support_chat_id,
            message_thread_id=message.message_thread_id,
            text="Выберите причину закрытия тикета:",
            reply_markup=close_reason_keyboard(ticket.id),
        )

    await callback.answer("Выберите причину закрытия.")


@router.callback_query(F.data.startswith(CALLBACK_CLOSE_REASON_PREFIX))
async def close_ticket_with_reason(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
    platform_router: PlatformRouter | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer("Сообщение с кнопкой недоступно.", show_alert=True)
        return
    if message.chat.id != settings.support_chat_id:
        await callback.answer("Кнопка работает только в группе поддержки.", show_alert=True)
        return
    if message.message_thread_id is None:
        await callback.answer("Кнопка работает только внутри топика тикета.", show_alert=True)
        return
    if not await is_support_chat_member(bot, settings.support_chat_id, callback.from_user.id):
        await callback.answer("Закрывать тикеты могут только участники группы поддержки.", show_alert=True)
        return

    parsed = parse_close_reason_callback(callback.data)
    if parsed is None:
        await callback.answer("Некорректная причина закрытия тикета.", show_alert=True)
        return
    ticket_id, reason = parsed

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    error = validate_ticket_for_callback(ticket, message.message_thread_id)
    if error:
        await callback.answer(error, show_alert=True)
        return

    assert ticket is not None
    closed = await close_ticket(bot, ticket_service, ticket, callback.from_user.id, reason, ticket_form_service, platform_router)
    if not closed:
        await callback.answer("Тикет уже закрыт.", show_alert=True)
        return

    try:
        await bot.edit_message_reply_markup(
            chat_id=settings.support_chat_id,
            message_id=message.message_id,
            reply_markup=closed_ticket_keyboard(),
        )
    except TelegramAPIError as error:
        logger.error("Failed to replace close button ticket_id=%s message_id=%s: %s", ticket.id, message.message_id, error)

    await callback.answer("Тикет закрыт.")


@router.callback_query(F.data == "ticket_closed")
async def closed_ticket_button(callback: CallbackQuery) -> None:
    await callback.answer("Тикет уже закрыт.", show_alert=True)


async def get_command_user(message: Message, session: AsyncSession, settings: Settings) -> User | None:
    if message.chat.id != settings.support_chat_id:
        return None
    if message.message_thread_id is None:
        await message.answer("Команда работает только внутри топика пользователя.")
        return None

    user = await UserService(session).get_by_topic_id(message.message_thread_id)
    if user is None:
        await message.answer("Этот топик не найден в базе бота.")
        return None

    return user


async def close_ticket(
    bot: Bot,
    ticket_service: TicketService,
    ticket: Ticket,
    closed_by_telegram_id: int,
    reason: str,
    ticket_form_service: TicketFormService,
    platform_router: PlatformRouter | None = None,
) -> bool:
    return await ticket_service.close_ticket(
        bot=bot,
        ticket=ticket,
        reason=reason,
        closed_by_telegram_id=closed_by_telegram_id,
        ticket_forms=ticket_form_service.get_forms() if ticket_form_service.enabled else None,
        platform_router=platform_router,
    )


async def prompt_close_reason(message: Message, ticket: Ticket) -> None:
    await message.answer(
        f"Выберите причину закрытия тикета #{ticket.id}:",
        reply_markup=support_close_reason_keyboard(),
    )


def get_close_reason_code_by_button_text(text: str) -> str | None:
    return next(
        (
            reason_code
            for reason_code in MANUAL_CLOSE_REASON_CODES
            if CLOSE_REASONS[reason_code].button_text == text
        ),
        None,
    )


async def is_support_chat_member(bot: Bot, support_chat_id: int, telegram_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=support_chat_id, user_id=telegram_id)
    except TelegramAPIError as error:
        logger.error("Failed to check support chat member telegram_id=%s: %s", telegram_id, error)
        return False
    return member.status not in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


def parse_ticket_id(callback_data: str | None) -> int | None:
    if not callback_data or not callback_data.startswith(CALLBACK_CLOSE_PREFIX):
        return None
    raw_ticket_id = callback_data.removeprefix(CALLBACK_CLOSE_PREFIX)
    try:
        return int(raw_ticket_id)
    except ValueError:
        return None


def parse_close_reason_callback(callback_data: str | None) -> tuple[int, str] | None:
    if not callback_data or not callback_data.startswith(CALLBACK_CLOSE_REASON_PREFIX):
        return None

    raw_payload = callback_data.removeprefix(CALLBACK_CLOSE_REASON_PREFIX)
    raw_ticket_id, separator, reason = raw_payload.partition(":")
    if not separator or reason not in MANUAL_CLOSE_REASON_CODES:
        return None

    try:
        ticket_id = int(raw_ticket_id)
    except ValueError:
        return None

    return ticket_id, reason


def validate_ticket_for_callback(ticket: Ticket | None, topic_id: int) -> str | None:
    if ticket is None:
        return "Тикет не найден."
    if ticket.topic_id != topic_id:
        return "Тикет не относится к этому топику."
    if ticket.status == TicketStatus.CLOSED:
        return "Тикет уже закрыт."
    if ticket.status != TicketStatus.OPEN:
        return "Тикет не открыт."
    return None


async def notify_user(
    bot: Bot,
    support_chat_id: int,
    user: User,
    text: str,
    error_text: str,
    platform_router: PlatformRouter | None = None,
) -> None:
    if platform_router is not None:
        sent = await platform_router.send_text(user, text, telegram_bot=bot)
        if sent is not None:
            return

    if user.telegram_id is None:
        await send_to_topic(bot, support_chat_id, user.topic_id, error_text)
        return

    try:
        await bot.send_message(chat_id=user.telegram_id, text=text)
    except TelegramAPIError as error:
        logger.error("Failed to notify telegram_id=%s: %s", user.telegram_id, error)
        await send_to_topic(bot, support_chat_id, user.topic_id, error_text)


async def send_to_topic(bot: Bot, support_chat_id: int, topic_id: int | None, text: str) -> None:
    if topic_id is None:
        return
    await bot.send_message(chat_id=support_chat_id, message_thread_id=topic_id, text=text)


def format_username(user: User) -> str:
    if not user.username:
        return "нет username"
    if user.platform == "telegram":
        return f"@{str(user.username).strip('@')}"
    return str(user.username).strip()


def format_platform(user: User) -> str:
    names = {"telegram": "Telegram", "discord": "Discord", "vk": "VK"}
    return names.get(user.platform, user.platform)


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "не указано"
    return value.isoformat(sep=" ", timespec="seconds")
