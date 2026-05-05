from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, InputMediaVideo, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.config import Settings
from app.bot.database.models import MessageDirection, Ticket, TicketStatus, User
from app.bot.keyboards import (
    CALLBACK_CANCEL_TICKET,
    CALLBACK_CONTINUE_MEDIA,
    CALLBACK_FORM_PREFIX,
    CALLBACK_OPEN_TICKET,
    CALLBACK_PROFILE_NICKNAME_OTHER,
    CALLBACK_PROFILE_NICKNAME_YES,
    CALLBACK_RESTART_TICKET,
    CALLBACK_SKIP_QUESTION,
    CALLBACK_SUBMIT_TICKET,
    minecraft_nickname_keyboard,
    multiple_media_keyboard,
    open_ticket_keyboard,
    question_keyboard,
    support_close_ticket_keyboard,
    ticket_forms_keyboard,
    ticket_forms_reply_keyboard,
    ticket_summary_keyboard,
)
from app.bot.services.message_service import MessageService, TopicUnavailableError
from app.bot.services.ticket_form_service import (
    ANSWER_TYPE_ANY,
    ANSWER_TYPE_MEDIA,
    ANSWER_TYPE_TEXT,
    TicketForm,
    TicketFormService,
    TicketQuestion,
    is_minecraft_nickname_question,
    validate_profile_text_answer,
)
from app.bot.services.ticket_formatter import TicketFormatter
from app.bot.services.ticket_service import TicketService
from app.bot.services.topic_service import TopicCreationError, TopicService
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)
router = Router(name="user_messages")

START_TEXT = (
    "Здравствуйте! Здесь вы можете обратиться в поддержку.\n\n"
    "Выберите тип обращения ниже."
)
OPEN_TICKET_EXISTS_TEXT = (
    "У вас уже есть открытый тикет.\n"
    "Вы можете просто написать сообщение сюда, и поддержка его увидит."
)
BLOCKED_TEXT = "Вы заблокированы службой поддержки."
NO_OPEN_TICKET_TEXT = "Чтобы обратиться в поддержку, откройте тикет."
CANCELLED_TEXT = "Заполнение тикета отменено."
FORMS_DISABLED_TEXT = "Система форм тикетов сейчас отключена. Попробуйте позже."
DELIVERY_ERROR_TEXT = (
    "Сейчас не удалось передать обращение в поддержку. "
    "Пожалуйста, попробуйте позже или сообщите администратору группы поддержки."
)
MEDIA_GROUP_BUFFER_DELAY_SECONDS = 1.0
MEDIA_GROUP_PROCESSED_TTL_SECONDS = 300.0
MEDIA_GROUP_BUFFERS: dict[tuple[int, str], dict[str, Any]] = {}
MEDIA_GROUP_TASKS: dict[tuple[int, str], asyncio.Task[None]] = {}
MEDIA_GROUP_PROCESSED_AT: dict[tuple[int, str], float] = {}
MEDIA_GROUP_LOCK = asyncio.Lock()
MEDIA_GROUP_COMPATIBLE_TYPES = {"photo", "video"}
CONTINUE_MEDIA_LOCKS: dict[int, asyncio.Lock] = {}


class TicketFlow(StatesGroup):
    profile_choice = State()
    answering = State()
    confirming = State()


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
    is_blocked_user: bool = False,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    await state.clear()
    user = await get_or_create_user(session, message.from_user, known_user)
    if is_blocked_user or user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    await show_ticket_forms(message, ticket_form_service)


@router.callback_query(F.data == CALLBACK_OPEN_TICKET)
async def open_ticket(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Открыть тикет можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user, known_user)
    if user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    if not ticket_form_service.enabled:
        await message.answer(FORMS_DISABLED_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    await show_ticket_forms(message, ticket_form_service)
    await callback.answer()


@router.callback_query(F.data.startswith(CALLBACK_FORM_PREFIX))
async def choose_form(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Выбрать форму можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user, known_user)
    if user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    if not ticket_form_service.enabled:
        await message.answer(FORMS_DISABLED_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    form_id = callback.data.removeprefix(CALLBACK_FORM_PREFIX) if callback.data else ""
    form = ticket_form_service.get_form(form_id)
    if form is None:
        await callback.answer("Форма не найдена. Попробуйте открыть список заново.", show_alert=True)
        return

    await start_form_flow(message, state, form, user, settings)
    await callback.answer()


@router.callback_query(StateFilter(TicketFlow.profile_choice), F.data == CALLBACK_PROFILE_NICKNAME_YES)
async def use_saved_minecraft_nickname(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user, known_user)
    applied = await apply_saved_minecraft_nickname(message, state, ticket_form_service, user, settings)
    await callback.answer("" if applied else "Действие устарело.", show_alert=not applied)


@router.callback_query(StateFilter(TicketFlow.profile_choice), F.data == CALLBACK_PROFILE_NICKNAME_OTHER)
async def enter_other_minecraft_nickname(
    callback: CallbackQuery,
    state: FSMContext,
    ticket_form_service: TicketFormService,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    question_index = int(data.get("question_index", 0))
    if form is None or question_index >= len(form.questions):
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    await state.set_state(TicketFlow.answering)
    await ask_current_question(message, form, question_index)
    await callback.answer()


@router.message(StateFilter(TicketFlow.profile_choice), F.chat.type == ChatType.PRIVATE)
async def handle_minecraft_nickname_choice_text(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if is_command_message(message):
        return

    user = await get_or_create_user(session, message.from_user, known_user)
    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    question_index = int(data.get("question_index", 0))
    text = (message.text or "").strip().casefold()

    if text in {"да", "yes", "y"}:
        if await apply_saved_minecraft_nickname(message, state, ticket_form_service, user, settings):
            return
        await message.answer("Это действие уже неактуально.")
        return

    if text in {"ввести другой", "другой", "нет", "no"}:
        if form is None or question_index >= len(form.questions):
            await state.clear()
            await show_ticket_forms(message, ticket_form_service)
            return
        await state.set_state(TicketFlow.answering)
        await ask_current_question(message, form, question_index)
        return

    if text in {"отмена", "cancel"}:
        if form is not None:
            await TicketService(session, settings.support_chat_id).create_cancelled_ticket(
                user,
                form,
                list(data.get("answers", [])),
            )
        await state.clear()
        await message.answer(
            CANCELLED_TEXT,
            reply_markup=ticket_forms_reply_keyboard(ticket_form_service.get_forms()) if ticket_form_service.enabled else None,
        )
        return

    await message.answer(
        "Напишите: Да или Ввести другой.",
        reply_markup=minecraft_nickname_keyboard(),
    )


@router.message(StateFilter(TicketFlow.answering), F.chat.type == ChatType.PRIVATE)
async def handle_form_answer(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
    is_blocked_user: bool = False,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if is_command_message(message):
        return

    user = await get_or_create_user(session, message.from_user, known_user)
    if is_blocked_user or user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await show_ticket_forms(message, ticket_form_service)
        return

    question_index = int(data.get("question_index", 0))
    if question_index >= len(form.questions):
        await state.clear()
        await show_ticket_forms(message, ticket_form_service)
        return
    question = form.questions[question_index]
    raw_answer = extract_ticket_answer(message)

    if raw_answer and raw_answer.get("file_id") and message.media_group_id and not question_accepts_multiple_media(question):
        should_process = await should_process_single_media_group_once(message)
        if not should_process:
            return

    if (
        question_accepts_multiple_media(question)
        and get_current_media_count(list(data.get("answers", [])), question) > 0
        and not (raw_answer and raw_answer.get("file_id"))
    ):
        media_count = get_current_media_count(list(data.get("answers", [])), question)
        max_files = question.max_files
        await message.answer(
            build_multiple_media_confirmation_text(
                media_count,
                max_files,
                max_files is not None and media_count >= max_files,
            ),
            reply_markup=multiple_media_keyboard(question_index),
        )
        return

    if question_accepts_multiple_media(question) and raw_answer and raw_answer.get("file_id"):
        media_item = build_media_item(
            raw_answer=raw_answer,
            source_message_id=message.message_id,
            media_group_id=message.media_group_id,
        )
        if message.media_group_id:
            await buffer_multiple_media_group(
                message=message,
                state=state,
                ticket_form_service=ticket_form_service,
                media_item=media_item,
            )
            return

        await process_multiple_media_items(
            message=message,
            state=state,
            ticket_form_service=ticket_form_service,
            media_items=[media_item],
        )
        return

    answer, validation_error = validate_question_answer(question, raw_answer)
    if validation_error:
        await message.answer(
            f"{validation_error}\n\n{build_question_text(question)}",
            reply_markup=get_question_reply_markup(question, question_index),
        )
        return

    answer.update(
        {
            "question_id": question.id,
            "question_text": question.text,
            "source_message_id": message.message_id,
        }
    )
    await maybe_save_minecraft_nickname(session, user, question, answer)
    answers = list(data.get("answers", []))
    answers.append(answer)
    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1, user=user, settings=settings)


@router.callback_query(StateFilter(TicketFlow.answering), F.data == CALLBACK_SKIP_QUESTION)
async def skip_question(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    question_index = int(data.get("question_index", 0))
    if question_index >= len(form.questions):
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return
    question = form.questions[question_index]
    if question.required:
        await callback.answer("Это обязательный вопрос.", show_alert=True)
        return

    answers = list(data.get("answers", []))
    answers.append(build_skipped_answer(question))
    user = await get_or_create_user(session, callback.from_user, known_user)
    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1, user=user, settings=settings)
    await callback.answer()


@router.callback_query(
    StateFilter(TicketFlow.answering),
    (F.data == CALLBACK_CONTINUE_MEDIA) | F.data.startswith(f"{CALLBACK_CONTINUE_MEDIA}:"),
)
async def continue_multiple_media_question(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    async with get_continue_media_lock(callback.from_user.id):
        user = await get_or_create_user(session, callback.from_user, known_user)
        await handle_continue_multiple_media_question(callback, state, ticket_form_service, message, user, settings)


async def handle_continue_multiple_media_question(
    callback: CallbackQuery,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    message: Message,
    user: User,
    settings: Settings,
) -> None:
    if await has_pending_media_group(callback.from_user.id):
        await callback.answer("Подождите, файлы ещё добавляются.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    question_index = int(data.get("question_index", 0))
    if question_index >= len(form.questions):
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    callback_question_index = parse_continue_media_question_index(callback.data)
    if callback_question_index is not None and callback_question_index != question_index:
        await callback.answer("Действие устарело.", show_alert=True)
        return

    question = form.questions[question_index]
    if not question_accepts_multiple_media(question):
        await callback.answer("Действие устарело.", show_alert=True)
        return

    answers = list(data.get("answers", []))
    media_count = get_current_media_count(answers, question)
    if media_count == 0:
        if question.required:
            await message.answer(
                f"Пожалуйста, прикрепите файл.\n\n{build_question_text(question)}",
                reply_markup=multiple_media_keyboard(question_index),
            )
            await callback.answer()
            return

        answers.append(build_skipped_answer(question))

    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1, user=user, settings=settings)
    await callback.answer()


@router.callback_query(StateFilter(TicketFlow.confirming), F.data == CALLBACK_RESTART_TICKET)
async def restart_form(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    await state.set_state(TicketFlow.answering)
    await state.update_data(answers=[], question_index=0)
    user = await get_or_create_user(session, callback.from_user, known_user)
    await ask_question_entry(message, state, form, 0, user, settings)
    await callback.answer()


@router.callback_query(F.data == CALLBACK_CANCEL_TICKET)
async def cancel_ticket_fill(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    user = await get_or_create_user(session, callback.from_user, known_user)
    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket and form is None:
        await callback.answer("Тикет уже отправлен.", show_alert=True)
        return
    if open_ticket:
        await state.clear()
        await callback.answer("У вас уже есть открытый тикет.", show_alert=True)
        return

    if form is not None:
        answers = list(data.get("answers", []))
        await ticket_service.create_cancelled_ticket(user, form, answers)

    await state.clear()
    await message.answer(
        CANCELLED_TEXT,
        reply_markup=ticket_forms_reply_keyboard(ticket_form_service.get_forms()) if ticket_form_service.enabled else None,
    )
    await callback.answer()


@router.callback_query(StateFilter(TicketFlow.confirming), F.data == CALLBACK_SUBMIT_TICKET)
async def submit_ticket(
    callback: CallbackQuery,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Отправить тикет можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user, known_user)
    if user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    answers = list(data.get("answers", []))
    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await state.clear()
        await message.answer(OPEN_TICKET_EXISTS_TEXT, reply_markup=ReplyKeyboardRemove())
        await callback.answer()
        return

    user_service = UserService(session)
    topic_service = TopicService(user_service, settings.support_chat_id)
    message_service = MessageService(session, settings.support_chat_id)

    ticket: Ticket | None = None
    admin_message_sent = False
    try:
        topic_id, _ = await topic_service.ensure_topic(bot, user)
        ticket = await ticket_service.create_open_ticket(user, form, answers, topic_id)
        try:
            await send_ticket_to_support(
                bot=bot,
                settings=settings,
                ticket_service=ticket_service,
                message_service=message_service,
                user=user,
                ticket=ticket,
                form=form,
                answers=answers,
            )
        except TelegramBadRequest as error:
            if not MessageService.is_topic_unavailable_error(error):
                raise
            topic_id, _ = await topic_service.recreate_topic(bot, user)
            await ticket_service.set_topic_id(ticket, topic_id)
            await send_ticket_to_support(
                bot=bot,
                settings=settings,
                ticket_service=ticket_service,
                message_service=message_service,
                user=user,
                ticket=ticket,
                form=form,
                answers=answers,
            )
        admin_message_sent = True
    except TopicCreationError as error:
        logger.error("Topic creation failed for telegram_id=%s: %s", user.telegram_id, error)
        await message.answer(DELIVERY_ERROR_TEXT)
        await callback.answer()
        return
    except TopicUnavailableError as error:
        logger.error("Topic unavailable while submitting ticket telegram_id=%s: %s", user.telegram_id, error)
        await message.answer(DELIVERY_ERROR_TEXT)
        await callback.answer()
        return
    except TelegramAPIError as error:
        logger.error("Failed to submit ticket telegram_id=%s: %s", user.telegram_id, error)
        if ticket is not None and not admin_message_sent:
            ticket.status = TicketStatus.CANCELLED
            await session.flush()
        await message.answer(DELIVERY_ERROR_TEXT)
        await callback.answer()
        return

    await state.clear()
    assert ticket is not None
    await message.answer(
        TicketService.build_success_text(form, ticket, user),
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.answer()


@router.callback_query(
    F.data.in_(
        {
            CALLBACK_RESTART_TICKET,
            CALLBACK_SKIP_QUESTION,
            CALLBACK_SUBMIT_TICKET,
            CALLBACK_PROFILE_NICKNAME_YES,
            CALLBACK_PROFILE_NICKNAME_OTHER,
        }
    )
    | (F.data == CALLBACK_CONTINUE_MEDIA)
    | F.data.startswith(f"{CALLBACK_CONTINUE_MEDIA}:")
)
async def stale_ticket_action(callback: CallbackQuery) -> None:
    await callback.answer("Действие устарело. Откройте тикет заново.", show_alert=True)


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    known_user: User | None = None,
    is_blocked_user: bool = False,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if is_command_message(message):
        return

    user_service = UserService(session)
    user = await get_or_create_user(session, message.from_user, known_user)
    if is_blocked_user or user.blocked:
        await message.answer(BLOCKED_TEXT, reply_markup=ReplyKeyboardRemove())
        logger.info("Ignored message from blocked user telegram_id=%s", user.telegram_id)
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    selected_form = get_form_by_button_text(ticket_form_service, message.text or "")
    if selected_form is not None:
        if ticket is not None:
            await message.answer(OPEN_TICKET_EXISTS_TEXT, reply_markup=ReplyKeyboardRemove())
            return
        if not ticket_form_service.enabled:
            await message.answer(FORMS_DISABLED_TEXT, reply_markup=ReplyKeyboardRemove())
            return
        await start_form_flow(message, state, selected_form, user, settings)
        return

    if ticket is None:
        await message.answer(
            NO_OPEN_TICKET_TEXT,
            reply_markup=ticket_forms_reply_keyboard(ticket_form_service.get_forms()) if ticket_form_service.enabled else None,
        )
        return

    topic_service = TopicService(user_service, settings.support_chat_id)
    message_service = MessageService(session, settings.support_chat_id)

    try:
        _, _ = await topic_service.ensure_topic(bot, user)
        if ticket.topic_id != user.topic_id and user.topic_id is not None:
            await ticket_service.set_topic_id(ticket, user.topic_id)
        try:
            await message_service.copy_user_message_to_support(bot, message, user, ticket)
        except TopicUnavailableError:
            topic_id, _ = await topic_service.recreate_topic(bot, user)
            await ticket_service.set_topic_id(ticket, topic_id)
            await message_service.copy_user_message_to_support(bot, message, user, ticket)
        await ticket_service.mark_user_activity(ticket)
    except TopicCreationError as error:
        logger.error("Topic creation failed for telegram_id=%s: %s", user.telegram_id, error)
        await message.answer(DELIVERY_ERROR_TEXT)
    except TopicUnavailableError as error:
        logger.error("Topic is unavailable for telegram_id=%s after retry: %s", user.telegram_id, error)
        await message.answer(DELIVERY_ERROR_TEXT)
    except TelegramAPIError as error:
        logger.error("Failed to deliver user message telegram_id=%s: %s", user.telegram_id, error)
        await message.answer(DELIVERY_ERROR_TEXT)


@router.edited_message(F.chat.type == ChatType.PRIVATE)
async def handle_private_edited_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    known_user: User | None = None,
    is_blocked_user: bool = False,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return
    if is_command_message(message):
        return

    user_service = UserService(session)
    user = known_user or await user_service.get_by_telegram_id(message.from_user.id)
    if user is None or user.blocked or is_blocked_user or not user.topic_id:
        return

    ticket = await TicketService(session, settings.support_chat_id).get_open_ticket_by_user_id(user.id)
    if ticket is None:
        return

    await MessageService(session, settings.support_chat_id).edit_user_message_copy_in_support(bot, message, user)


async def move_to_next_question_or_summary(
    message: Message,
    state: FSMContext,
    form: TicketForm,
    answers: list[dict[str, Any]],
    next_question_index: int,
    *,
    user: User | None = None,
    settings: Settings | None = None,
) -> None:
    if next_question_index < len(form.questions):
        await state.update_data(answers=answers, question_index=next_question_index)
        if user is not None and settings is not None:
            await ask_question_entry(message, state, form, next_question_index, user, settings)
        else:
            await ask_current_question(message, form, next_question_index)
        return

    await state.set_state(TicketFlow.confirming)
    await state.update_data(answers=answers, question_index=next_question_index)
    await message.answer(
        TicketService.build_user_summary_text(form, answers),
        reply_markup=ticket_summary_keyboard(),
    )


async def ask_current_question(message: Message, form: TicketForm, question_index: int) -> None:
    question = form.questions[question_index]
    await message.answer(build_question_text(question), reply_markup=get_question_reply_markup(question, question_index))


async def ask_question_entry(
    message: Message,
    state: FSMContext,
    form: TicketForm,
    question_index: int,
    user: User,
    settings: Settings,
) -> None:
    question = form.questions[question_index]
    if should_offer_minecraft_nickname(settings, user, question):
        await state.set_state(TicketFlow.profile_choice)
        await state.update_data(question_index=question_index)
        await message.answer(
            f"Использовать прошлый ник {user.minecraft_nickname}?",
            reply_markup=minecraft_nickname_keyboard(),
        )
        return

    await state.set_state(TicketFlow.answering)
    await ask_current_question(message, form, question_index)


async def start_form_flow(message: Message, state: FSMContext, form: TicketForm, user: User, settings: Settings) -> None:
    await state.set_state(TicketFlow.answering)
    await state.update_data(form_id=form.id, answers=[], question_index=0)
    await message.answer("Начинаем заполнение тикета.", reply_markup=ReplyKeyboardRemove())
    await ask_question_entry(message, state, form, 0, user, settings)


async def show_ticket_forms(message: Message, ticket_form_service: TicketFormService) -> None:
    if not ticket_form_service.enabled:
        await message.answer(FORMS_DISABLED_TEXT, reply_markup=ReplyKeyboardRemove())
        return
    await message.answer(
        START_TEXT,
        reply_markup=ticket_forms_reply_keyboard(ticket_form_service.get_forms()),
    )


async def send_ticket_to_support(
    bot: Bot,
    settings: Settings,
    ticket_service: TicketService,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    form: TicketForm,
    answers: list[dict[str, Any]],
) -> None:
    if ticket.topic_id is None:
        raise TopicUnavailableError("Ticket has no topic_id")

    card_parts = TicketFormatter.build_new_ticket_parts(ticket, user, form, answers)
    card_message = await send_topic_message(
        bot=bot,
        settings=settings,
        topic_id=ticket.topic_id,
        text=card_parts[0],
        parse_mode=ParseMode.HTML,
    )
    await ticket_service.set_card_message_id(ticket, card_message.message_id)
    await ticket_service.pin_ticket_card(bot, ticket)

    for extra_part in card_parts[1:]:
        await send_topic_message(
            bot=bot,
            settings=settings,
            topic_id=ticket.topic_id,
            text=extra_part,
            parse_mode=ParseMode.HTML,
        )

    await send_ticket_media_to_support(bot, message_service, user, ticket, form, answers)

    control_message = await send_topic_message(
        bot=bot,
        settings=settings,
        topic_id=ticket.topic_id,
        text=TicketFormatter.build_control_text(ticket.id),
        reply_markup=support_close_ticket_keyboard(),
    )
    await ticket_service.set_control_message_id(ticket, control_message.message_id)


async def send_ticket_media_to_support(
    bot: Bot,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    form: TicketForm,
    answers: list[dict[str, Any]],
) -> None:
    for question_number, question, media_items in TicketFormatter.iter_question_media_groups(form, answers):
        if len(media_items) > 1 and media_items_can_be_sent_as_group(media_items):
            try:
                await send_question_media_group_to_support(
                    bot=bot,
                    message_service=message_service,
                    user=user,
                    ticket=ticket,
                    question_number=question_number,
                    question=question,
                    media_items=media_items,
                )
                continue
            except TelegramAPIError as error:
                logger.error(
                    "Failed to send ticket media group ticket_id=%s question_id=%s: %s",
                    ticket.id,
                    question.id,
                    error,
                )
            except TopicUnavailableError as error:
                logger.error("Failed to send ticket media group because topic is unavailable ticket_id=%s: %s", ticket.id, error)

        await send_question_media_individually_to_support(
            bot=bot,
            message_service=message_service,
            user=user,
            ticket=ticket,
            question_number=question_number,
            question=question,
            media_items=media_items,
        )


async def send_question_media_group_to_support(
    bot: Bot,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    question_number: int,
    question: TicketQuestion,
    media_items: list[Any],
) -> None:
    if ticket.topic_id is None:
        raise TopicUnavailableError("Ticket has no topic_id")

    media_total = len(media_items)
    first_caption = TicketFormatter.build_media_group_caption(
        question_number=question_number,
        question_text=question.text,
        media_total=media_total,
        user_caption=media_items[0].get("caption"),
    )
    input_media = []
    for media_index, media in enumerate(media_items):
        media_type = media.get("media_type")
        media_kwargs: dict[str, Any] = {"media": str(media.get("file_id"))}
        if media_index == 0:
            media_kwargs["caption"] = first_caption
        if media_type == "photo":
            input_media.append(InputMediaPhoto(**media_kwargs))
        elif media_type == "video":
            input_media.append(InputMediaVideo(**media_kwargs))

    if not input_media:
        return

    try:
        sent_messages = await bot.send_media_group(
            chat_id=message_service.support_chat_id,
            message_thread_id=ticket.topic_id,
            media=input_media,
        )
    except TelegramBadRequest as error:
        if MessageService.is_topic_unavailable_error(error):
            raise TopicUnavailableError(str(error)) from error
        raise

    for media, sent_message in zip(media_items, sent_messages, strict=False):
        source_message_id = parse_optional_int(media.get("source_message_id"))
        if source_message_id is None:
            continue
        await message_service.create_message_map(
            user=user,
            user_message_id=source_message_id,
            support_message_id=sent_message.message_id,
            topic_id=ticket.topic_id,
            direction=MessageDirection.TICKET_FORM_MEDIA,
            ticket_id=ticket.id,
        )

    captions_text = TicketFormatter.build_media_captions_text(media_items)
    if captions_text:
        try:
            await bot.send_message(
                chat_id=message_service.support_chat_id,
                message_thread_id=ticket.topic_id,
                text=captions_text,
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to send ticket media captions text ticket_id=%s question_id=%s: %s",
                ticket.id,
                question.id,
                error,
            )


async def send_question_media_individually_to_support(
    bot: Bot,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    question_number: int,
    question: TicketQuestion,
    media_items: list[Any],
) -> None:
    media_total = len(media_items)
    for media_index, media in enumerate(media_items, start=1):
        source_message_id = media.get("source_message_id")
        if not source_message_id:
            logger.error(
                "Ticket form media has no source message id ticket_id=%s question_id=%s media_index=%s",
                ticket.id,
                question.id,
                media_index,
            )
            continue
        caption = TicketFormatter.build_media_caption(
            question_number=question_number,
            question_text=question.text,
            media_index=media_index,
            media_total=media_total,
            user_caption=media.get("caption"),
            media_type=media.get("media_type"),
        )
        try:
            await message_service.copy_ticket_form_media_to_support(
                bot,
                user,
                ticket,
                int(source_message_id),
                caption=caption,
            )
        except TopicUnavailableError as error:
            logger.error("Failed to copy ticket media because topic is unavailable ticket_id=%s: %s", ticket.id, error)


def media_items_can_be_sent_as_group(media_items: list[Any]) -> bool:
    if len(media_items) < 2:
        return False
    return all(
        media.get("file_id") and media.get("media_type") in MEDIA_GROUP_COMPATIBLE_TYPES
        for media in media_items
    )


async def send_topic_message(
    bot: Bot,
    settings: Settings,
    topic_id: int,
    text: str,
    **kwargs: Any,
) -> Message:
    try:
        return await bot.send_message(
            chat_id=settings.support_chat_id,
            message_thread_id=topic_id,
            text=text,
            **kwargs,
        )
    except TelegramBadRequest as error:
        if MessageService.is_topic_unavailable_error(error):
            raise
        raise


def get_question_reply_markup(question: TicketQuestion, question_index: int) -> Any:
    if question_accepts_multiple_media(question):
        return multiple_media_keyboard(question_index)
    return question_keyboard(question.required)


def question_accepts_multiple_media(question: TicketQuestion) -> bool:
    return question.allow_multiple and question.answer_type in {ANSWER_TYPE_MEDIA, ANSWER_TYPE_ANY}


async def buffer_multiple_media_group(
    message: Message,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    media_item: dict[str, Any],
) -> None:
    if message.from_user is None or not message.media_group_id:
        return

    key = (message.from_user.id, message.media_group_id)
    async with MEDIA_GROUP_LOCK:
        cleanup_processed_media_groups()
        if key in MEDIA_GROUP_PROCESSED_AT:
            return

        buffer = MEDIA_GROUP_BUFFERS.setdefault(
            key,
            {
                "message": message,
                "state": state,
                "ticket_form_service": ticket_form_service,
                "items": [],
            },
        )
        buffer["items"].append(media_item)
        if key not in MEDIA_GROUP_TASKS:
            MEDIA_GROUP_TASKS[key] = asyncio.create_task(flush_multiple_media_group(key))


async def flush_multiple_media_group(key: tuple[int, str]) -> None:
    try:
        await asyncio.sleep(MEDIA_GROUP_BUFFER_DELAY_SECONDS)
        async with MEDIA_GROUP_LOCK:
            buffer = MEDIA_GROUP_BUFFERS.pop(key, None)
            MEDIA_GROUP_TASKS.pop(key, None)
            MEDIA_GROUP_PROCESSED_AT[key] = time.monotonic()

        if not buffer:
            return

        message = buffer["message"]
        state = buffer["state"]
        ticket_form_service = buffer["ticket_form_service"]
        media_items = sorted(
            buffer["items"],
            key=lambda item: (parse_optional_int(item.get("sort_order")) or 0, parse_optional_int(item.get("source_message_id")) or 0),
        )
        await process_multiple_media_items(
            message=message,
            state=state,
            ticket_form_service=ticket_form_service,
            media_items=media_items,
        )
    except Exception as error:
        logger.exception("Failed to process media group key=%s: %s", key, error)
        async with MEDIA_GROUP_LOCK:
            MEDIA_GROUP_BUFFERS.pop(key, None)
            MEDIA_GROUP_TASKS.pop(key, None)


async def should_process_single_media_group_once(message: Message) -> bool:
    if message.from_user is None or not message.media_group_id:
        return True

    key = (message.from_user.id, message.media_group_id)
    async with MEDIA_GROUP_LOCK:
        cleanup_processed_media_groups()
        if key in MEDIA_GROUP_PROCESSED_AT or key in MEDIA_GROUP_BUFFERS:
            return False
        MEDIA_GROUP_PROCESSED_AT[key] = time.monotonic()
        return True


async def has_pending_media_group(user_id: int) -> bool:
    async with MEDIA_GROUP_LOCK:
        return any(key_user_id == user_id for key_user_id, _ in MEDIA_GROUP_BUFFERS)


def cleanup_processed_media_groups() -> None:
    now = time.monotonic()
    stale_keys = [
        key
        for key, processed_at in MEDIA_GROUP_PROCESSED_AT.items()
        if now - processed_at > MEDIA_GROUP_PROCESSED_TTL_SECONDS
    ]
    for key in stale_keys:
        MEDIA_GROUP_PROCESSED_AT.pop(key, None)


async def process_multiple_media_items(
    message: Message,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    media_items: list[dict[str, Any]],
) -> None:
    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        logger.info("Ignored media group because form state is missing")
        return

    question_index = int(data.get("question_index", 0))
    if question_index >= len(form.questions):
        logger.info("Ignored media group because question state is finished form_id=%s", form.id)
        return

    question = form.questions[question_index]
    if not question_accepts_multiple_media(question):
        logger.info(
            "Ignored stale media group for non-multiple question form_id=%s question_id=%s",
            form.id,
            question.id,
        )
        return

    answers = list(data.get("answers", []))
    current_count = get_current_media_count(answers, question)
    max_files = question.max_files if question.max_files is not None and question.max_files > 0 else None
    if max_files is not None and current_count >= max_files:
        await message.answer(
            build_multiple_media_limit_text(max_files),
            reply_markup=multiple_media_keyboard(question_index),
        )
        return

    accepted_items = media_items
    if max_files is not None:
        accepted_items = media_items[: max_files - current_count]

    for media_item in accepted_items:
        answers = append_media_to_current_answer(answers, question, media_item)

    if accepted_items:
        await state.update_data(answers=answers)

    media_count = get_current_media_count(answers, question)
    limit_reached = max_files is not None and media_count >= max_files
    await message.answer(
        build_multiple_media_confirmation_text(media_count, max_files, limit_reached),
        reply_markup=multiple_media_keyboard(question_index),
    )


def build_media_item(
    raw_answer: dict[str, Any],
    source_message_id: int,
    media_group_id: str | None = None,
) -> dict[str, Any]:
    return {
        "file_id": raw_answer.get("file_id"),
        "media_type": raw_answer.get("media_type"),
        "caption": raw_answer.get("caption"),
        "source_message_id": source_message_id,
        "media_group_id": media_group_id,
        "sort_order": source_message_id,
    }


def append_media_to_current_answer(
    answers: list[dict[str, Any]],
    question: TicketQuestion,
    media_item: dict[str, Any],
) -> list[dict[str, Any]]:
    answer = find_answer_for_question(answers, question.id)
    if answer is None:
        answer = {
            "question_id": question.id,
            "question_text": question.text,
            "answer_type": ANSWER_TYPE_MEDIA,
            "answer_text": None,
            "file_id": media_item["file_id"],
            "media_type": media_item["media_type"],
            "caption": media_item["caption"],
            "source_message_id": media_item.get("source_message_id"),
            "media_group_id": media_item.get("media_group_id"),
            "sort_order": media_item.get("sort_order"),
            "media_files": [],
            "skipped": False,
        }
        answers.append(answer)
    answer.setdefault("media_files", [])
    answer["media_files"].append(media_item)
    if not answer.get("file_id"):
        answer["file_id"] = media_item["file_id"]
        answer["media_type"] = media_item["media_type"]
        answer["caption"] = media_item["caption"]
        answer["source_message_id"] = media_item.get("source_message_id")
        answer["media_group_id"] = media_item.get("media_group_id")
        answer["sort_order"] = media_item.get("sort_order")
    return answers


def build_multiple_media_confirmation_text(
    media_count: int,
    max_files: int | None,
    limit_reached: bool = False,
) -> str:
    if max_files is None:
        return (
            f"Файлы добавлены: {media_count}.\n"
            "Можно отправить ещё файл или нажать «Продолжить»."
        )
    if limit_reached:
        return (
            f"Файлы добавлены: {media_count} из {max_files}.\n"
            "Достигнут лимит файлов.\n\n"
            "Нажмите «Продолжить», чтобы перейти дальше."
        )
    return (
        f"Файлы добавлены: {media_count} из {max_files}.\n"
        "Можно отправить ещё файл или нажать «Продолжить»."
    )


def build_multiple_media_limit_text(max_files: int) -> str:
    return (
        f"Достигнут лимит файлов: {max_files}.\n"
        "Нажмите «Продолжить», чтобы перейти дальше."
    )


def parse_optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_continue_media_question_index(callback_data: str | None) -> int | None:
    if not callback_data or ":" not in callback_data:
        return None
    prefix, raw_index = callback_data.split(":", maxsplit=1)
    if prefix != CALLBACK_CONTINUE_MEDIA:
        return None
    return parse_optional_int(raw_index)


def get_continue_media_lock(user_id: int) -> asyncio.Lock:
    lock = CONTINUE_MEDIA_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        CONTINUE_MEDIA_LOCKS[user_id] = lock
    return lock


def get_current_media_count(answers: list[dict[str, Any]], question: TicketQuestion) -> int:
    answer = find_answer_for_question(answers, question.id)
    if answer is None:
        return 0
    return len(TicketService.extract_media_files(answer))


def find_answer_for_question(answers: list[dict[str, Any]], question_id: str) -> dict[str, Any] | None:
    return next((answer for answer in answers if answer.get("question_id") == question_id), None)


def get_form_by_button_text(ticket_form_service: TicketFormService, text: str) -> TicketForm | None:
    text = text.strip()
    return next((form for form in ticket_form_service.get_forms() if form.button_text == text), None)


async def send_admin_ticket_message(
    bot: Bot,
    settings: Settings,
    topic_id: int,
    ticket_id: int,
    text: str,
) -> None:
    await bot.send_message(
        chat_id=settings.support_chat_id,
        message_thread_id=topic_id,
        text=text,
        reply_markup=support_close_ticket_keyboard(),
    )


def get_state_form(ticket_form_service: TicketFormService, data: dict[str, Any]) -> TicketForm | None:
    form_id = data.get("form_id")
    if not isinstance(form_id, str):
        return None
    return ticket_form_service.get_form(form_id)


async def get_or_create_user(
    session: AsyncSession,
    telegram_user: Any,
    known_user: User | None = None,
) -> User:
    if known_user is not None:
        return known_user

    user, _ = await UserService(session).get_or_create_from_telegram(telegram_user)
    return user


def should_offer_minecraft_nickname(settings: Settings, user: User, question: TicketQuestion) -> bool:
    return (
        settings.minecraft_nickname_autofill_enabled
        and is_minecraft_nickname_question(question)
        and bool(str(user.minecraft_nickname or "").strip())
    )


async def apply_saved_minecraft_nickname(
    message: Message,
    state: FSMContext,
    ticket_form_service: TicketFormService,
    user: User,
    settings: Settings,
) -> bool:
    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    question_index = int(data.get("question_index", 0))
    if form is None or question_index >= len(form.questions):
        await state.clear()
        return False

    question = form.questions[question_index]
    nickname = str(user.minecraft_nickname or "").strip()
    if not nickname or not is_minecraft_nickname_question(question):
        return False

    validation_error = validate_profile_text_answer(question, nickname)
    if validation_error:
        await state.set_state(TicketFlow.answering)
        await message.answer(
            f"{validation_error}\n\n{build_question_text(question)}",
            reply_markup=get_question_reply_markup(question, question_index),
        )
        return True

    answers = list(data.get("answers", []))
    answers.append(build_text_profile_answer(question, nickname))
    await move_to_next_question_or_summary(
        message,
        state,
        form,
        answers,
        question_index + 1,
        user=user,
        settings=settings,
    )
    return True


async def maybe_save_minecraft_nickname(
    session: AsyncSession,
    user: User,
    question: TicketQuestion,
    answer: dict[str, Any],
) -> None:
    if not is_minecraft_nickname_question(question):
        return
    if answer.get("skipped"):
        return
    if answer.get("answer_type") != ANSWER_TYPE_TEXT:
        return
    nickname = str(answer.get("answer_text") or "").strip()
    if not nickname:
        return
    if user.minecraft_nickname == nickname:
        return
    await UserService(session).set_minecraft_nickname(user, nickname)


def extract_ticket_answer(message: Message) -> dict[str, Any] | None:
    media = extract_media(message)
    if media is not None:
        media_type, file_id = media
        return {
            "answer_type": "media",
            "answer_text": None,
            "file_id": file_id,
            "media_type": media_type,
            "caption": message.caption,
        }

    text = MessageService.extract_text_or_caption(message)
    if text is not None:
        return {
            "answer_type": "text",
            "answer_text": text.strip(),
            "file_id": None,
            "media_type": None,
            "caption": None,
        }

    return None


def validate_question_answer(
    question: TicketQuestion,
    answer: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None]:
    if answer is None:
        return {}, "Этот тип сообщения не поддерживается. Отправьте текст или медиа."

    if is_minecraft_nickname_question(question):
        text = str(answer.get("answer_text") or answer.get("caption") or "").strip()
        if not text:
            return {}, "Пожалуйста, отправьте текст."
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
        normalized_answer = dict(answer)
        normalized_answer.update(
            {
                "answer_type": ANSWER_TYPE_TEXT,
                "answer_text": text,
                "file_id": None,
                "media_type": None,
                "caption": None,
                "skipped": False,
            }
        )
        return normalized_answer, None

    expected_answer_type = question.answer_type or ANSWER_TYPE_ANY
    if expected_answer_type == ANSWER_TYPE_TEXT:
        text = str(answer.get("answer_text") or answer.get("caption") or "").strip()
        if not text:
            return {}, "Пожалуйста, отправьте текст."

        normalized_answer = dict(answer)
        normalized_answer.update(
            {
                "answer_type": ANSWER_TYPE_TEXT,
                "answer_text": text,
                "file_id": None,
                "media_type": None,
                "caption": None,
                "skipped": False,
            }
        )
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
        return normalized_answer, None

    if expected_answer_type == ANSWER_TYPE_MEDIA:
        if not answer.get("file_id"):
            return {}, "Пожалуйста, прикрепите файл, фото, видео или другое медиа."

        normalized_answer = dict(answer)
        normalized_answer["skipped"] = False
        return normalized_answer, None

    if not answer_has_value(answer):
        return {}, "Отправьте текст или медиа."

    normalized_answer = dict(answer)
    if normalized_answer.get("answer_type") == ANSWER_TYPE_TEXT:
        text = str(normalized_answer.get("answer_text") or "").strip()
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
    normalized_answer["skipped"] = False
    return normalized_answer, None


def build_text_profile_answer(question: TicketQuestion, text: str) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": ANSWER_TYPE_TEXT,
        "answer_text": text,
        "file_id": None,
        "media_type": None,
        "caption": None,
        "skipped": False,
    }


def build_skipped_answer(question: TicketQuestion) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": question.answer_type or ANSWER_TYPE_ANY,
        "answer_text": None,
        "file_id": None,
        "media_type": None,
        "caption": None,
        "skipped": True,
    }


def build_question_text(question: TicketQuestion) -> str:
    if not question.help_text:
        return question.text
    return f"{question.text}\n\nПодсказка: {question.help_text}"


def extract_media(message: Message) -> tuple[str, str] | None:
    if message.photo:
        return "photo", message.photo[-1].file_id
    if message.video:
        return "video", message.video.file_id
    if message.document:
        return "document", message.document.file_id
    if message.voice:
        return "voice", message.voice.file_id
    if message.audio:
        return "audio", message.audio.file_id
    if message.animation:
        return "animation", message.animation.file_id
    if message.sticker:
        return "sticker", message.sticker.file_id
    return None


def answer_has_value(answer: dict[str, Any]) -> bool:
    if answer.get("file_id"):
        return True
    return bool(str(answer.get("answer_text") or "").strip())


def is_command_message(message: Message) -> bool:
    text = MessageService.extract_text_or_caption(message)
    return bool(text and text.lstrip().startswith("/"))
