from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.config import Settings
from app.bot.database.models import Ticket, TicketStatus, User
from app.bot.keyboards import (
    CALLBACK_CANCEL_TICKET,
    CALLBACK_FORM_PREFIX,
    CALLBACK_OPEN_TICKET,
    CALLBACK_RESTART_TICKET,
    CALLBACK_SKIP_QUESTION,
    CALLBACK_SUBMIT_TICKET,
    close_ticket_keyboard,
    open_ticket_keyboard,
    question_keyboard,
    ticket_forms_keyboard,
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
from app.bot.services.ticket_service import TicketService
from app.bot.services.topic_service import TopicCreationError, TopicService
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)
router = Router(name="user_messages")

START_TEXT = (
    "Здравствуйте! Здесь вы можете обратиться в поддержку.\n\n"
    "Нажмите кнопку ниже, чтобы открыть тикет."
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


class TicketFlow(StatesGroup):
    answering = State()
    confirming = State()


@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
async def start(
    message: Message,
    session: AsyncSession,
    settings: Settings,
    state: FSMContext,
    known_user: User | None = None,
    is_blocked_user: bool = False,
) -> None:
    if message.from_user is None or message.from_user.is_bot:
        return

    await state.clear()
    user = await get_or_create_user(session, message.from_user, known_user)
    if is_blocked_user or user.blocked:
        await message.answer(BLOCKED_TEXT)
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT)
        return

    await message.answer(START_TEXT, reply_markup=open_ticket_keyboard())


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
        await message.answer(BLOCKED_TEXT)
        await callback.answer()
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT)
        await callback.answer()
        return

    if not ticket_form_service.enabled:
        await message.answer(FORMS_DISABLED_TEXT)
        await callback.answer()
        return

    await message.answer("Выберите тип обращения:", reply_markup=ticket_forms_keyboard(ticket_form_service.get_forms()))
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
        await message.answer(BLOCKED_TEXT)
        await callback.answer()
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if open_ticket:
        await message.answer(OPEN_TICKET_EXISTS_TEXT)
        await callback.answer()
        return

    if not ticket_form_service.enabled:
        await message.answer(FORMS_DISABLED_TEXT)
        await callback.answer()
        return

    form_id = callback.data.removeprefix(CALLBACK_FORM_PREFIX) if callback.data else ""
    form = ticket_form_service.get_form(form_id)
    if form is None:
        await callback.answer("Форма не найдена. Попробуйте открыть список заново.", show_alert=True)
        return

    await state.set_state(TicketFlow.answering)
    await state.update_data(form_id=form.id, answers=[], question_index=0)
    await ask_current_question(message, form, 0)
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
        await message.answer(BLOCKED_TEXT)
        return

    data = await state.get_data()
    form = get_state_form(ticket_form_service, data)
    if form is None:
        await state.clear()
        await message.answer(START_TEXT, reply_markup=open_ticket_keyboard())
        return

    question_index = int(data.get("question_index", 0))
    if question_index >= len(form.questions):
        await state.clear()
        await message.answer(START_TEXT, reply_markup=open_ticket_keyboard())
        return
    question = form.questions[question_index]
    raw_answer = extract_ticket_answer(message)
    answer, validation_error = validate_question_answer(question, raw_answer)
    if validation_error:
        await message.answer(
            f"{validation_error}\n\n{build_question_text(question)}",
            reply_markup=question_keyboard(question.required),
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
    await message.answer(CANCELLED_TEXT)
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
        await message.answer(BLOCKED_TEXT)
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
        await message.answer(OPEN_TICKET_EXISTS_TEXT)
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
        admin_text = TicketService.build_admin_ticket_text(form, user, answers)
        try:
            await send_admin_ticket_message(bot, settings, topic_id, ticket.id, admin_text)
        except TelegramBadRequest as error:
            if not MessageService.is_topic_unavailable_error(error):
                raise
            topic_id, _ = await topic_service.recreate_topic(bot, user)
            await ticket_service.set_topic_id(ticket, topic_id)
            await send_admin_ticket_message(bot, settings, topic_id, ticket.id, admin_text)
        admin_message_sent = True
        for answer in answers:
            if answer.get("answer_type") == "media" and answer.get("file_id") and answer.get("source_message_id"):
                try:
                    await message_service.copy_ticket_form_media_to_support(
                        bot,
                        user,
                        ticket,
                        int(answer["source_message_id"]),
                    )
                except TopicUnavailableError as error:
                    logger.error("Failed to copy ticket media because topic is unavailable ticket_id=%s: %s", ticket.id, error)
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
    await message.answer(TICKET_SENT_TEXT)
    await callback.answer()


@router.callback_query(F.data.in_({CALLBACK_RESTART_TICKET, CALLBACK_SKIP_QUESTION, CALLBACK_SUBMIT_TICKET}))
async def stale_ticket_action(callback: CallbackQuery) -> None:
    await callback.answer("Действие устарело. Откройте тикет заново.", show_alert=True)


@router.message(F.chat.type == ChatType.PRIVATE)
async def handle_private_message(
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
    user = await get_or_create_user(session, message.from_user, known_user)
    if is_blocked_user or user.blocked:
        await message.answer(BLOCKED_TEXT)
        logger.info("Ignored message from blocked user telegram_id=%s", user.telegram_id)
        return

    ticket_service = TicketService(session, settings.support_chat_id)
    ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
    if ticket is None:
        await message.answer(NO_OPEN_TICKET_TEXT, reply_markup=open_ticket_keyboard())
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
    await message.answer(build_question_text(question), reply_markup=question_keyboard(question.required))


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
        reply_markup=close_ticket_keyboard(ticket_id),
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
