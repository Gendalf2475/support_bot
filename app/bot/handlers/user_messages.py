from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.config import Settings
from app.bot.database.models import Ticket, TicketStatus, User
from app.bot.keyboards import (
    CALLBACK_CANCEL_TICKET,
    CALLBACK_CONTINUE_MEDIA,
    CALLBACK_FORM_PREFIX,
    CALLBACK_OPEN_TICKET,
    CALLBACK_RESTART_TICKET,
    CALLBACK_SKIP_QUESTION,
    CALLBACK_SUBMIT_TICKET,
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
TICKET_SENT_TEXT = "✅ Тикет отправлен в поддержку.\nОтвет придёт в этот чат."
FORMS_DISABLED_TEXT = "Система форм тикетов сейчас отключена. Попробуйте позже."
DELIVERY_ERROR_TEXT = (
    "Сейчас не удалось передать обращение в поддержку. "
    "Пожалуйста, попробуйте позже или сообщите администратору группы поддержки."
)
MULTIPLE_MEDIA_ADDED_TEXT = "Файл добавлен.\nМожно отправить ещё файл или нажать «Продолжить»."


class TicketFlow(StatesGroup):
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
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Открыть тикет можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user)
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
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Выбрать форму можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user)
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

    await start_form_flow(message, state, form)
    await callback.answer()


@router.message(StateFilter(TicketFlow.answering), F.chat.type == ChatType.PRIVATE)
async def handle_form_answer(
    message: Message,
    session: AsyncSession,
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
    if (
        question_accepts_multiple_media(question)
        and get_current_media_count(list(data.get("answers", [])), question) > 0
        and not (raw_answer and raw_answer.get("file_id"))
    ):
        await message.answer(MULTIPLE_MEDIA_ADDED_TEXT, reply_markup=multiple_media_keyboard())
        return

    if question_accepts_multiple_media(question) and raw_answer and raw_answer.get("file_id"):
        answers = append_media_to_current_answer(
            list(data.get("answers", [])),
            question,
            raw_answer,
            message.message_id,
        )
        media_count = get_current_media_count(answers, question)
        if media_count >= question.max_files:
            await message.answer(f"Достигнут лимит файлов: {question.max_files}.")
            await move_to_next_question_or_summary(message, state, form, answers, question_index + 1)
            return

        await state.update_data(answers=answers)
        await message.answer(MULTIPLE_MEDIA_ADDED_TEXT, reply_markup=multiple_media_keyboard())
        return

    answer, validation_error = validate_question_answer(question, raw_answer)
    if validation_error:
        await message.answer(
            f"{validation_error}\n\n{build_question_text(question)}",
            reply_markup=get_question_reply_markup(question),
        )
        return

    answer.update(
        {
            "question_id": question.id,
            "question_text": question.text,
            "source_message_id": message.message_id,
        }
    )
    answers = list(data.get("answers", []))
    answers.append(answer)
    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1)


@router.callback_query(StateFilter(TicketFlow.answering), F.data == CALLBACK_SKIP_QUESTION)
async def skip_question(
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
    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1)
    await callback.answer()


@router.callback_query(StateFilter(TicketFlow.answering), F.data == CALLBACK_CONTINUE_MEDIA)
async def continue_multiple_media_question(
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
    if not question_accepts_multiple_media(question):
        await callback.answer("Действие устарело.", show_alert=True)
        return

    answers = list(data.get("answers", []))
    media_count = get_current_media_count(answers, question)
    if media_count == 0:
        if question.required:
            await message.answer(
                f"Пожалуйста, прикрепите файл.\n\n{build_question_text(question)}",
                reply_markup=multiple_media_keyboard(),
            )
            await callback.answer()
            return

        answers.append(build_skipped_answer(question))

    await move_to_next_question_or_summary(message, state, form, answers, question_index + 1)
    await callback.answer()


@router.callback_query(StateFilter(TicketFlow.confirming), F.data == CALLBACK_RESTART_TICKET)
async def restart_form(
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
    if form is None:
        await state.clear()
        await callback.answer("Форма устарела. Откройте тикет заново.", show_alert=True)
        return

    await state.set_state(TicketFlow.answering)
    await state.update_data(answers=[], question_index=0)
    await ask_current_question(message, form, 0)
    await callback.answer()


@router.callback_query(F.data == CALLBACK_CANCEL_TICKET)
async def cancel_ticket_fill(
    callback: CallbackQuery,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    ticket_form_service: TicketFormService,
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Действие доступно только в личном чате с ботом.", show_alert=True)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    user = await get_or_create_user(session, callback.from_user)
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
) -> None:
    message = callback.message
    if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Отправить тикет можно только в личном чате с ботом.", show_alert=True)
        return

    user = await get_or_create_user(session, callback.from_user)
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
    await message.answer(TICKET_SENT_TEXT, reply_markup=ReplyKeyboardRemove())
    await callback.answer()


@router.callback_query(F.data.in_({CALLBACK_RESTART_TICKET, CALLBACK_SKIP_QUESTION, CALLBACK_SUBMIT_TICKET, CALLBACK_CONTINUE_MEDIA}))
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
        await start_form_flow(message, state, selected_form)
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
) -> None:
    if next_question_index < len(form.questions):
        await state.update_data(answers=answers, question_index=next_question_index)
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
    await message.answer(build_question_text(question), reply_markup=get_question_reply_markup(question))


async def start_form_flow(message: Message, state: FSMContext, form: TicketForm) -> None:
    await state.set_state(TicketFlow.answering)
    await state.update_data(form_id=form.id, answers=[], question_index=0)
    await message.answer("Начинаем заполнение тикета.", reply_markup=ReplyKeyboardRemove())
    await ask_current_question(message, form, 0)


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
    for question_number, question, media_index, media_total, media in TicketFormatter.iter_media_answers(form, answers):
        source_message_id = media.get("source_message_id")
        if not source_message_id:
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


def get_question_reply_markup(question: TicketQuestion) -> Any:
    if question_accepts_multiple_media(question):
        return multiple_media_keyboard()
    return question_keyboard(question.required)


def question_accepts_multiple_media(question: TicketQuestion) -> bool:
    return question.allow_multiple and question.answer_type in {ANSWER_TYPE_MEDIA, ANSWER_TYPE_ANY}


def append_media_to_current_answer(
    answers: list[dict[str, Any]],
    question: TicketQuestion,
    raw_answer: dict[str, Any],
    source_message_id: int,
) -> list[dict[str, Any]]:
    media_item = {
        "file_id": raw_answer.get("file_id"),
        "media_type": raw_answer.get("media_type"),
        "caption": raw_answer.get("caption"),
        "source_message_id": source_message_id,
    }
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
            "source_message_id": source_message_id,
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
        answer["source_message_id"] = source_message_id
    return answers


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
    if known_user is None:
        user, _ = await UserService(session).get_or_create_from_telegram(telegram_user)
        return user
    user, _ = await UserService(session).get_or_create_from_telegram(telegram_user)
    return user


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
    normalized_answer["skipped"] = False
    return normalized_answer, None


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
