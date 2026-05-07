from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.bot.config import Settings
from app.bot.constants.actions import ACTION_TICKET_CLOSED
from app.bot.database.models import Ticket, TicketStatus, User, utcnow
from app.bot.keyboards import (
    CALLBACK_CLOSE_PREFIX,
    CALLBACK_CLOSE_REASON_PREFIX,
    close_reason_keyboard,
    closed_ticket_keyboard,
    SUPPORT_CLOSE_TICKET_TEXT,
)
from app.bot.services.minecraft_service import MinecraftService, PlayerLookupResult
from app.bot.services.ticket_form_service import TicketFormService
from app.bot.services.ticket_service import (
    ACTIVE_TICKET_STATUSES,
    CLOSE_REASONS,
    FORCE_CLOSED_REASON,
    MANUAL_CLOSE_REASON_CODES,
    TicketService,
)
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
        f"Minecraft-ник: {user.minecraft_nickname or 'не указан'}\n"
        f"Topic ID: {user.topic_id}\n"
        f"Blocked: {user.blocked}\n"
        f"Открытый тикет: #{open_ticket.id if open_ticket else 'нет'}\n"
        f"Статус тикета: {open_ticket.status.value if open_ticket else 'нет'}\n"
        f"ticket_created_at: {format_dt(open_ticket.created_at) if open_ticket else 'нет'}\n"
        f"created_at: {format_dt(user.created_at)}\n"
        f"updated_at: {format_dt(user.updated_at)}"
    )
    await send_to_topic(bot, settings.support_chat_id, user.topic_id, text)


@router.message(Command("lookup"))
async def lookup_player(message: Message, settings: Settings, minecraft_service: MinecraftService) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    if message.from_user is None:
        await message.answer("Не удалось определить, кто выполняет команду.")
        return

    nickname = parse_lookup_nickname(message.text or "")
    if not nickname:
        await message.answer("Использование: /lookup <nickname>")
        return

    logger.info("/lookup used by telegram_id=%s nickname=%s", message.from_user.id, nickname)
    if not minecraft_service.is_enabled():
        await message.answer("Minecraft API выключен в настройках.")
        return

    result = await minecraft_service.check_player(nickname, respect_check_enabled=False)
    if result.error == "disabled":
        await message.answer("Minecraft API выключен в настройках.")
        return
    if result.exists is None and result.error:
        await message.answer(f"Не удалось проверить игрока: {result.error}")
        return

    await message.answer(format_player_lookup_result(result))


@router.message(Command("health"))
async def health(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    channel_supervisor: Any | None = None,
    ticket_scheduler: Any | None = None,
) -> None:
    if message.chat.id != settings.support_chat_id:
        return

    database_status, database_error = await check_database_health(session)
    lines = ["🩺 Health", ""]
    lines.extend(format_channel_health(channel_supervisor, settings))
    lines.append(format_scheduler_health(ticket_scheduler))
    lines.append(f"Database: {database_status}" + (f" ({database_error})" if database_error else ""))
    await message.answer("\n".join(lines))


@router.message(Command("debug_user"))
async def debug_user(message: Message, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    parsed = parse_platform_user_command(message.text or "")
    if parsed is None:
        await message.answer("Использование: /debug_user <platform> <platform_user_id>")
        return

    platform, platform_user_id = parsed
    user = await UserService(session).get_by_platform_user_id(platform, platform_user_id)
    if user is None:
        await message.answer("Пользователь не найден.")
        return

    statement = (
        select(Ticket)
        .where(Ticket.user_id == user.id)
        .order_by(Ticket.created_at.desc())
        .limit(10)
    )
    tickets = list((await session.scalars(statement)).all())
    await message.answer(format_debug_user_text(user, tickets))


@router.message(Command("force_close_user"))
async def force_close_user(
    message: Message,
    bot: Bot,
    dispatcher: Dispatcher,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    if message.from_user is None:
        await message.answer("Не удалось определить администратора.")
        return
    parsed = parse_platform_user_command(message.text or "")
    if parsed is None:
        await message.answer("Использование: /force_close_user <platform> <platform_user_id>")
        return

    platform, platform_user_id = parsed
    user = await UserService(session).get_by_platform_user_id(platform, platform_user_id)
    if user is None:
        await message.answer("Пользователь не найден.")
        return

    statement = (
        select(Ticket)
        .where(Ticket.user_id == user.id, Ticket.status.in_(ACTIVE_TICKET_STATUSES))
        .options(selectinload(Ticket.answers))
        .order_by(Ticket.created_at.desc())
    )
    tickets = list((await session.scalars(statement)).all())
    now = utcnow()
    for ticket in tickets:
        logger.info("Force closing ticket id=%s current_status=%s", ticket.id, ticket.status.value)
        ticket.status = TicketStatus.CLOSED
        ticket.close_reason = FORCE_CLOSED_REASON
        ticket.closed_at = now
        ticket.closed_by_telegram_id = message.from_user.id
        ticket.updated_at = now
    if platform_router is not None:
        platform_router.clear_user_state(user.platform, user.platform_user_id)
    if user.platform == "telegram":
        telegram_id = parse_int(user.platform_user_id)
        if telegram_id is not None:
            fsm_context = await dispatcher.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
            await fsm_context.clear()

    await session.flush()
    await session.commit()
    ticket_service = TicketService(session, settings.support_chat_id)
    for ticket in tickets:
        await ticket_service.update_ticket_card(bot, ticket, user, CLOSE_REASONS[FORCE_CLOSED_REASON].label)
        await ticket_service.update_ticket_control_message(bot, ticket)
    await message.answer(f"Активные тикеты пользователя закрыты: {len(tickets)}")


@router.message(Command("debug_state"))
async def debug_state(
    message: Message,
    bot: Bot,
    dispatcher: Dispatcher,
    session: AsyncSession,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    parsed = parse_platform_user_command(message.text or "")
    if parsed is None:
        await message.answer("Использование: /debug_state <platform> <platform_user_id>")
        return

    platform, platform_user_id = parsed
    user = await UserService(session).get_by_platform_user_id(platform, platform_user_id)
    active_ticket = await TicketService(session, settings.support_chat_id).get_open_ticket_by_user_id(user.id) if user else None
    state_info = await read_user_state(
        bot=bot,
        dispatcher=dispatcher,
        platform_router=platform_router,
        platform=platform,
        platform_user_id=platform_user_id,
    )
    await message.answer(format_debug_state_text(platform, platform_user_id, state_info, active_ticket))


@router.message(Command("reset_state"))
async def reset_state(
    message: Message,
    bot: Bot,
    dispatcher: Dispatcher,
    settings: Settings,
    platform_router: PlatformRouter | None = None,
) -> None:
    if message.chat.id != settings.support_chat_id:
        return
    parsed = parse_platform_user_command(message.text or "")
    if parsed is None:
        await message.answer("Использование: /reset_state <platform> <platform_user_id>")
        return

    platform, platform_user_id = parsed
    if platform == "telegram":
        telegram_id = parse_int(platform_user_id)
        if telegram_id is not None:
            fsm_context = await dispatcher.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
            await fsm_context.clear()
    elif platform_router is not None:
        platform_router.clear_user_state(platform, platform_user_id)

    await message.answer("Состояние пользователя сброшено.")


@router.message(Command("close"))
async def close_ticket_by_command(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
) -> None:
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
        latest_ticket = await ticket_service.get_latest_ticket_by_topic_id(message.message_thread_id)
        await message.answer("Тикет уже закрыт." if latest_ticket and latest_ticket.status not in ACTIVE_TICKET_STATUSES else "В этом топике нет открытого тикета.")
        return

    await prompt_close_reason(message, ticket, ticket_form_service)


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text == SUPPORT_CLOSE_TICKET_TEXT)
async def close_ticket_by_keyboard(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
) -> None:
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

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_topic_id(message.message_thread_id)
    if ticket is None:
        latest_ticket = await ticket_service.get_latest_ticket_by_topic_id(message.message_thread_id)
        await message.answer(
            "Тикет уже закрыт." if latest_ticket and latest_ticket.status in {TicketStatus.CLOSED, TicketStatus.CANCELLED} else "Нет открытого тикета для закрытия.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await prompt_close_reason(message, ticket, ticket_form_service)


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
        latest_ticket = await ticket_service.get_latest_ticket_by_topic_id(message.message_thread_id)
        await message.answer(
            "Тикет уже закрыт." if latest_ticket and latest_ticket.status in {TicketStatus.CLOSED, TicketStatus.CANCELLED} else "Нет открытого тикета для закрытия.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    try:
        closed = await close_ticket(
            bot=bot,
            ticket_service=ticket_service,
            ticket=ticket,
            closed_by_telegram_id=message.from_user.id,
            reason=reason,
            ticket_form_service=ticket_form_service,
            platform_router=platform_router,
        )
    except ValueError:
        await message.answer("Причина закрытия устарела. Нажмите «Закрыть тикет» ещё раз.")
        return
    if not closed:
        await message.answer("Тикет уже закрыт.")


@router.callback_query(F.data.startswith(CALLBACK_CLOSE_PREFIX))
async def close_ticket_by_callback(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
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
    reasons = TicketService.get_form_close_reasons(
        ticket,
        ticket_form_service.get_forms() if ticket_form_service.enabled else None,
    )
    try:
        await bot.edit_message_reply_markup(
            chat_id=settings.support_chat_id,
            message_id=message.message_id,
            reply_markup=close_reason_keyboard(ticket.id, reasons),
        )
    except TelegramAPIError as error:
        logger.error("Failed to show close reasons ticket_id=%s message_id=%s: %s", ticket.id, message.message_id, error)
        await bot.send_message(
            chat_id=settings.support_chat_id,
            message_thread_id=message.message_thread_id,
            text="Выберите причину закрытия тикета:",
            reply_markup=close_reason_keyboard(ticket.id, reasons),
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
    try:
        closed = await close_ticket(bot, ticket_service, ticket, callback.from_user.id, reason, ticket_form_service, platform_router)
    except ValueError:
        await callback.answer("Причина закрытия устарела. Откройте список причин заново.", show_alert=True)
        return
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


@router.callback_query(F.data == ACTION_TICKET_CLOSED)
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


async def prompt_close_reason(
    message: Message,
    ticket: Ticket,
    ticket_form_service: TicketFormService | None = None,
) -> None:
    ticket_forms = ticket_form_service.get_forms() if ticket_form_service and ticket_form_service.enabled else None
    reasons = TicketService.get_form_close_reasons(ticket, ticket_forms)
    await message.answer(
        f"Выберите причину закрытия тикета #{ticket.id}:",
        reply_markup=close_reason_keyboard(ticket.id, reasons),
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
    if not separator or not reason:
        return None

    try:
        ticket_id = int(raw_ticket_id)
    except ValueError:
        return None

    return ticket_id, reason


def parse_lookup_nickname(text: str) -> str | None:
    parts = str(text or "").strip().split(maxsplit=1)
    if len(parts) < 2:
        return None
    nickname = parts[1].strip()
    if " " in nickname:
        nickname = nickname.split(maxsplit=1)[0]
    return nickname or None


def parse_platform_user_command(text: str) -> tuple[str, str] | None:
    parts = str(text or "").strip().split(maxsplit=2)
    if len(parts) < 3:
        return None
    platform = parts[1].strip().lower()
    platform_user_id = parts[2].strip()
    if platform not in {"telegram", "discord", "vk"} or not platform_user_id:
        return None
    return platform, platform_user_id


def parse_int(value: str | None) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def read_user_state(
    *,
    bot: Bot,
    dispatcher: Dispatcher,
    platform_router: PlatformRouter | None,
    platform: str,
    platform_user_id: str,
) -> dict[str, Any]:
    if platform == "telegram":
        telegram_id = parse_int(platform_user_id)
        if telegram_id is None:
            return {"state": "invalid_telegram_id"}
        fsm_context = await dispatcher.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
        current_state = await fsm_context.get_state()
        data = await fsm_context.get_data()
        answers = data.get("answers")
        media_files = data.get("current_media_files")
        return {
            "state": current_state or "none",
            "selected_form_id": data.get("form_id"),
            "current_question_index": data.get("question_index"),
            "current_question_id": data.get("current_question_id"),
            "answers_count": len(answers) if isinstance(answers, list) else 0,
            "media_count": len(media_files) if isinstance(media_files, list) else count_media_in_answers(data),
            "pending_action": detect_telegram_pending_action(current_state, data),
            "fsm_data_keys": ", ".join(sorted(data.keys())) or "нет",
        }

    if platform_router is None:
        return {"state": "platform_router_unavailable"}
    state_info = platform_router.get_user_state(platform, platform_user_id)
    return state_info or {"state": "none"}


def count_media_in_answers(data: dict[str, Any]) -> int:
    answers = data.get("answers")
    current_question_id = data.get("current_question_id")
    if not isinstance(answers, list) or not current_question_id:
        return 0
    for answer in answers:
        if isinstance(answer, dict) and answer.get("question_id") == current_question_id:
            media_files = answer.get("media_files")
            if isinstance(media_files, list):
                return len(media_files)
            return 1 if answer.get("file_id") else 0
    return 0


def detect_telegram_pending_action(current_state: str | None, data: dict[str, Any]) -> str | None:
    if current_state and current_state.endswith(":profile_choice"):
        return "nickname_choice"
    if current_state and current_state.endswith(":profile_change_confirm"):
        return "nickname_change_confirmation"
    if current_state and current_state.endswith(":minecraft_lookup_confirm"):
        return "minecraft_lookup_confirmation"
    if current_state and current_state.endswith(":collecting_media"):
        return "media_continue"
    if current_state and current_state.endswith(":confirming"):
        return "ticket_preview"
    if data.get("pending_media_question_id"):
        return "media_continue"
    if data.get("pending_minecraft_answer"):
        return "minecraft_lookup_confirmation"
    return None


def format_debug_state_text(
    platform: str,
    platform_user_id: str,
    state_info: dict[str, Any],
    active_ticket: Ticket | None,
) -> str:
    lines = [
        "🧪 Debug state\n\n"
        f"platform: {platform}\n"
        f"platform_user_id: {platform_user_id}\n"
        f"FSM/current state: {state_info.get('state', 'none')}\n"
        f"selected_form_id: {state_info.get('selected_form_id', 'нет')}\n"
        f"current_question_index: {state_info.get('current_question_index', 'нет')}\n"
        f"current_question_id: {state_info.get('current_question_id', 'нет')}\n"
        f"answers_count: {state_info.get('answers_count', 0)}\n"
        f"media_count: {state_info.get('media_count', 0)}\n"
        f"pending_action: {state_info.get('pending_action') or 'нет'}"
    ]
    if state_info.get("fsm_data_keys") is not None:
        lines.append(f"fsm_data_keys: {state_info.get('fsm_data_keys')}")
    if active_ticket:
        lines.append(f"active_ticket: #{active_ticket.id} {active_ticket.status.value}")
    else:
        lines.append("active_ticket: нет")
    return "\n".join(lines)


def format_debug_user_text(user: User, tickets: list[Ticket]) -> str:
    lines = [
        "🧪 Debug user",
        "",
        f"user.id: {user.id}",
        f"platform: {user.platform}",
        f"platform_user_id: {user.platform_user_id}",
        f"username: {user.username or 'не указан'}",
        f"full_name: {user.full_name or 'не указано'}",
        f"topic_id: {user.topic_id}",
        f"blocked: {user.blocked}",
        f"minecraft_nickname: {user.minecraft_nickname or 'не указан'}",
        f"minecraft_nickname_updated_at: {format_dt(user.minecraft_nickname_updated_at)}",
        "",
        "Последние тикеты:",
    ]
    if not tickets:
        lines.append("нет")
        return "\n".join(lines)

    for ticket in tickets:
        lines.append(
            (
                f"#{ticket.id} | {ticket.status.value} | {ticket.form_title} | "
                f"close_reason={ticket.close_reason or 'нет'} | "
                f"created_at={format_dt(ticket.created_at)} | closed_at={format_dt(ticket.closed_at)}"
            )
        )
    return "\n".join(lines)


def format_player_lookup_result(result: PlayerLookupResult) -> str:
    nickname = result.nickname or "не указан"
    lines = [
        "🎮 Информация об игроке",
        "",
        f"Ник: {nickname}",
    ]
    if result.exists is True:
        lines.append("Проверка: ✅ найден")
    elif result.exists is False:
        lines.append("Проверка: ❌ не найден")
    else:
        lines.append("Проверка: ⚠️ не удалось проверить")

    if result.uuid:
        lines.append(f"UUID: {result.uuid}")
    if result.online is not None:
        lines.append(f"Онлайн: {'да' if result.online else 'нет'}")
    if result.source:
        lines.append(f"Источник: {result.source}")
    if result.exists is None and result.error:
        lines.append(f"Ошибка: {result.error}")
    return "\n".join(lines)


def validate_ticket_for_callback(ticket: Ticket | None, topic_id: int) -> str | None:
    if ticket is None:
        return "Тикет не найден."
    if ticket.topic_id != topic_id:
        return "Тикет не относится к этому топику."
    if ticket.status == TicketStatus.CLOSED:
        return "Тикет уже закрыт."
    if ticket.status == TicketStatus.CANCELLED:
        return "Тикет отменён."
    if ticket.status not in ACTIVE_TICKET_STATUSES:
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


async def check_database_health(session: AsyncSession) -> tuple[str, str | None]:
    try:
        await session.execute(text("SELECT 1"))
        return "ok", None
    except Exception as error:
        logger.exception("Database health check failed: %s", error)
        return "error", f"{type(error).__name__}: {error}"


def format_channel_health(channel_supervisor: Any | None, settings: Settings) -> list[str]:
    statuses = {health.name: health for health in channel_supervisor.snapshot()} if channel_supervisor is not None else {}
    defaults = [
        ("telegram", "Telegram", settings.telegram_enabled),
        ("discord", "Discord", settings.discord_enabled),
        ("vk", "VK", settings.vk_enabled and settings.vk_longpoll_enabled),
    ]
    lines: list[str] = []
    for name, display_name, enabled in defaults:
        health = statuses.get(name)
        if health is None:
            status = "disabled" if not enabled else "unknown"
            lines.append(f"{display_name}: {status}")
            lines.append("Последняя ошибка: нет")
            lines.append("Последняя ошибка в: нет")
            lines.append("Ошибок подряд: 0")
            lines.append("Последнее восстановление: нет")
            lines.append("")
            continue

        lines.append(f"{display_name}: {health.status}")
        lines.append(f"Последняя ошибка: {health.last_error_type or 'нет'}" + (f" ({health.last_error_message})" if health.last_error_message else ""))
        lines.append(f"Последняя ошибка в: {format_dt(health.last_error_at)}")
        lines.append(f"Ошибок подряд: {health.consecutive_errors}")
        lines.append(f"Последнее восстановление: {format_dt(health.last_restored_at)}")
        lines.append("")
    return lines


def format_scheduler_health(ticket_scheduler: Any | None) -> str:
    if ticket_scheduler is None:
        return "Scheduler: unknown"
    if not ticket_scheduler.has_enabled_jobs():
        return "Scheduler: disabled"
    if ticket_scheduler.consecutive_errors:
        return (
            "Scheduler: error"
            f"\nПоследняя ошибка: {ticket_scheduler.last_error or 'нет'}"
            f"\nПоследняя ошибка в: {format_dt(ticket_scheduler.last_error_at)}"
            f"\nОшибок подряд: {ticket_scheduler.consecutive_errors}"
            f"\nПоследнее восстановление: {format_dt(ticket_scheduler.last_success_at)}"
        )
    if ticket_scheduler.scheduler.running:
        return (
            "Scheduler: working"
            f"\nПоследняя ошибка: нет"
            f"\nОшибок подряд: 0"
            f"\nПоследнее восстановление: {format_dt(ticket_scheduler.last_success_at)}"
        )
    return "Scheduler: error\nПоследняя ошибка: scheduler is not running"


def format_dt(value: datetime | None) -> str:
    if value is None:
        return "не указано"
    return value.isoformat(sep=" ", timespec="seconds")
