from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.channels.base import Attachment, IncomingMessage
from app.bot.config import Settings
from app.bot.database.models import MessageDirection, Ticket, User
from app.bot.keyboards import support_close_ticket_keyboard
from app.bot.services.message_service import MessageService, TopicUnavailableError
from app.bot.services.platform_router import PlatformRouter, build_external_forms_text
from app.bot.services.ticket_form_service import ANSWER_TYPE_ANY, ANSWER_TYPE_MEDIA, ANSWER_TYPE_TEXT, TicketForm, TicketFormService, TicketQuestion
from app.bot.services.ticket_formatter import TicketFormatter
from app.bot.services.ticket_service import TicketService
from app.bot.services.topic_service import TopicCreationError, TopicService
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)


@dataclass
class ExternalDraft:
    form: TicketForm
    question_index: int = 0
    answers: list[dict[str, Any]] = field(default_factory=list)
    confirming: bool = False


class ExternalSupportProcessor:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        ticket_form_service: TicketFormService,
        platform_router: PlatformRouter,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.ticket_form_service = ticket_form_service
        self.platform_router = platform_router
        self.drafts: dict[tuple[str, str], ExternalDraft] = {}

    async def handle_incoming(self, incoming: IncomingMessage) -> None:
        key = (incoming.platform, incoming.platform_user_id)
        async with self.sessionmaker() as session:
            user_service = UserService(session)
            user_result = await user_service.upsert_user(
                platform=incoming.platform,
                platform_user_id=incoming.platform_user_id,
                username=incoming.username,
                full_name=incoming.full_name,
            )
            user = user_result.user
            if user_result.changed and user.topic_id:
                await TopicService(user_service, self.settings.support_chat_id).sync_topic_title(self.bot, user)

            if user.blocked:
                await self.platform_router.send_text(user, "Вы заблокированы службой поддержки.", telegram_bot=self.bot)
                await session.commit()
                return

            ticket_service = TicketService(session, self.settings.support_chat_id)
            open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
            if open_ticket is not None:
                await self.forward_open_ticket_message(session, user, open_ticket, incoming)
                await ticket_service.mark_user_activity(open_ticket)
                await session.commit()
                return

            await self.handle_ticket_flow(session, user, incoming, key)
            await session.commit()

    async def forward_open_ticket_message(
        self,
        session: AsyncSession,
        user: User,
        ticket: Ticket,
        incoming: IncomingMessage,
    ) -> None:
        user_service = UserService(session)
        topic_service = TopicService(user_service, self.settings.support_chat_id)
        message_service = MessageService(session, self.settings.support_chat_id)
        try:
            await topic_service.ensure_topic(self.bot, user)
            await message_service.send_external_message_to_support(self.bot, incoming, user, ticket)
        except (TopicCreationError, TopicUnavailableError, TelegramAPIError) as error:
            logger.error(
                "Failed to forward external message platform=%s platform_user_id=%s: %s",
                incoming.platform,
                incoming.platform_user_id,
                error,
            )
            await self.platform_router.send_text(
                user,
                "Сейчас не удалось передать сообщение в поддержку. Попробуйте позже.",
                telegram_bot=self.bot,
            )

    async def handle_ticket_flow(
        self,
        session: AsyncSession,
        user: User,
        incoming: IncomingMessage,
        key: tuple[str, str],
    ) -> None:
        draft = self.drafts.get(key)
        text = (incoming.text or "").strip()

        if draft is None:
            form = self.resolve_form(text)
            if form is None:
                await self.platform_router.send_text(user, build_external_forms_text(self.ticket_form_service.get_forms()), telegram_bot=self.bot)
                return
            draft = ExternalDraft(form=form)
            self.drafts[key] = draft
            await self.platform_router.send_text(user, f"Начинаем заполнение тикета.\n\n{build_question_text(form.questions[0])}", telegram_bot=self.bot)
            return

        if text.casefold() in {"отмена", "cancel"}:
            self.drafts.pop(key, None)
            await self.platform_router.send_text(user, f"Заполнение тикета отменено.\n\n{build_external_forms_text(self.ticket_form_service.get_forms())}", telegram_bot=self.bot)
            return

        if draft.confirming:
            await self.handle_confirmation(session, user, incoming, key, draft)
            return

        await self.handle_question_answer(session, user, incoming, key, draft)

    async def handle_confirmation(
        self,
        session: AsyncSession,
        user: User,
        incoming: IncomingMessage,
        key: tuple[str, str],
        draft: ExternalDraft,
    ) -> None:
        text = (incoming.text or "").strip().casefold()
        if text in {"отправить", "send", "submit", "да"}:
            await self.submit_ticket(session, user, draft)
            self.drafts.pop(key, None)
            await self.platform_router.send_text(user, "✅ Тикет отправлен в поддержку. Ответ придёт сюда.", telegram_bot=self.bot)
            return

        if text in {"заново", "restart"}:
            self.drafts[key] = ExternalDraft(form=draft.form)
            await self.platform_router.send_text(user, build_question_text(draft.form.questions[0]), telegram_bot=self.bot)
            return

        if text in {"отмена", "cancel"}:
            self.drafts.pop(key, None)
            await self.platform_router.send_text(user, f"Заполнение тикета отменено.\n\n{build_external_forms_text(self.ticket_form_service.get_forms())}", telegram_bot=self.bot)
            return

        await self.platform_router.send_text(user, "Напишите: Отправить, Заново или Отмена.", telegram_bot=self.bot)

    async def handle_question_answer(
        self,
        session: AsyncSession,
        user: User,
        incoming: IncomingMessage,
        key: tuple[str, str],
        draft: ExternalDraft,
    ) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.show_summary(user, draft)
            return

        question = draft.form.questions[draft.question_index]
        text = (incoming.text or "").strip()
        if question.allow_multiple and question.answer_type in {ANSWER_TYPE_MEDIA, ANSWER_TYPE_ANY}:
            answer = find_answer(draft.answers, question.id)
            if text.casefold() in {"продолжить", "continue", "далее"}:
                media_count = len(TicketService.extract_media_files(answer or {}))
                if media_count == 0 and question.required:
                    await self.platform_router.send_text(user, f"Пожалуйста, прикрепите файл.\n\n{build_question_text(question)}", telegram_bot=self.bot)
                    return
                if media_count == 0:
                    draft.answers.append(build_skipped_answer(question))
                await self.advance_or_summary(user, draft)
                return

            if incoming.attachments:
                draft.answers = append_external_media(draft.answers, question, incoming.attachments)
                media_count = len(TicketService.extract_media_files(find_answer(draft.answers, question.id) or {}))
                if media_count >= question.max_files:
                    await self.advance_or_summary(user, draft)
                else:
                    await self.platform_router.send_text(
                        user,
                        f"Файлы добавлены: {media_count} из {question.max_files}.\nМожно отправить ещё файл или написать «Продолжить».",
                        telegram_bot=self.bot,
                    )
                return

        answer, error = validate_external_answer(question, incoming)
        if error:
            await self.platform_router.send_text(user, f"{error}\n\n{build_question_text(question)}", telegram_bot=self.bot)
            return

        draft.answers.append(answer)
        await self.advance_or_summary(user, draft)

    async def advance_or_summary(self, user: User, draft: ExternalDraft) -> None:
        draft.question_index += 1
        if draft.question_index < len(draft.form.questions):
            await self.platform_router.send_text(user, build_question_text(draft.form.questions[draft.question_index]), telegram_bot=self.bot)
            return
        await self.show_summary(user, draft)

    async def show_summary(self, user: User, draft: ExternalDraft) -> None:
        draft.confirming = True
        summary = TicketService.build_user_summary_text(draft.form, draft.answers)
        await self.platform_router.send_text(user, f"{summary}\n\nНапишите: Отправить, Заново или Отмена.", telegram_bot=self.bot)

    async def submit_ticket(self, session: AsyncSession, user: User, draft: ExternalDraft) -> None:
        ticket_service = TicketService(session, self.settings.support_chat_id)
        topic_service = TopicService(UserService(session), self.settings.support_chat_id)
        message_service = MessageService(session, self.settings.support_chat_id)

        topic_id, _ = await topic_service.ensure_topic(self.bot, user)
        ticket = await ticket_service.create_open_ticket(user, draft.form, draft.answers, topic_id)
        await publish_external_ticket_to_support(
            bot=self.bot,
            settings=self.settings,
            ticket_service=ticket_service,
            message_service=message_service,
            user=user,
            ticket=ticket,
            form=draft.form,
            answers=draft.answers,
        )

    def resolve_form(self, text: str) -> TicketForm | None:
        if not self.ticket_form_service.enabled:
            return None
        forms = self.ticket_form_service.get_forms()
        if not text:
            return None
        normalized = text.casefold()
        for index, form in enumerate(forms, start=1):
            if text == str(index) or normalized in {form.title.casefold(), form.button_text.casefold(), form.id.casefold()}:
                return form
        return None


async def publish_external_ticket_to_support(
    bot: Bot,
    settings: Settings,
    ticket_service: TicketService,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    form: TicketForm,
    answers: list[dict[str, Any]],
) -> None:
    card_parts = TicketFormatter.build_new_ticket_parts(ticket, user, form, answers)
    card_message = await bot.send_message(
        chat_id=settings.support_chat_id,
        message_thread_id=ticket.topic_id,
        text=card_parts[0],
        parse_mode=ParseMode.HTML,
    )
    await ticket_service.set_card_message_id(ticket, card_message.message_id)
    await ticket_service.pin_ticket_card(bot, ticket)
    for extra_part in card_parts[1:]:
        await bot.send_message(
            chat_id=settings.support_chat_id,
            message_thread_id=ticket.topic_id,
            text=extra_part,
            parse_mode=ParseMode.HTML,
        )

    await send_external_ticket_media(bot, settings, message_service, user, ticket, form, answers)
    control_message = await bot.send_message(
        chat_id=settings.support_chat_id,
        message_thread_id=ticket.topic_id,
        text=TicketFormatter.build_control_text(ticket.id),
        reply_markup=support_close_ticket_keyboard(),
    )
    await ticket_service.set_control_message_id(ticket, control_message.message_id)


async def send_external_ticket_media(
    bot: Bot,
    settings: Settings,
    message_service: MessageService,
    user: User,
    ticket: Ticket,
    form: TicketForm,
    answers: list[dict[str, Any]],
) -> None:
    if ticket.topic_id is None:
        return
    for question_number, question, media_items in TicketFormatter.iter_question_media_groups(form, answers):
        for index, media in enumerate(media_items, start=1):
            caption = TicketFormatter.build_media_caption(
                question_number=question_number,
                question_text=question.text,
                media_index=index,
                media_total=len(media_items),
                user_caption=media.get("caption"),
                media_type=media.get("media_type"),
            )
            url = media.get("file_url") or media.get("file_id")
            if not url:
                continue
            try:
                sent = await send_external_attachment_to_topic(bot, settings.support_chat_id, ticket.topic_id, media, url, caption)
            except TelegramAPIError as error:
                logger.error("Failed to send external attachment ticket_id=%s url=%s: %s", ticket.id, url, error)
                sent = await bot.send_message(
                    chat_id=settings.support_chat_id,
                    message_thread_id=ticket.topic_id,
                    text=f"{caption or 'Вложение'}\n{url}",
                )
            await message_service.create_message_map(
                user=user,
                user_message_id=None,
                support_message_id=sent.message_id,
                topic_id=ticket.topic_id,
                direction=MessageDirection.TICKET_FORM_MEDIA,
                ticket_id=ticket.id,
                platform=user.platform,
                platform_message_id=media.get("source_message_id"),
                telegram_support_message_id=sent.message_id,
            )


async def send_external_attachment_to_topic(
    bot: Bot,
    support_chat_id: int,
    topic_id: int,
    media: dict[str, Any],
    url: str,
    caption: str | None,
) -> Any:
    media_type = media.get("media_type")
    try:
        if media_type == "photo":
            return await bot.send_photo(chat_id=support_chat_id, message_thread_id=topic_id, photo=url, caption=caption)
        if media_type == "video":
            return await bot.send_video(chat_id=support_chat_id, message_thread_id=topic_id, video=url, caption=caption)
        return await bot.send_document(chat_id=support_chat_id, message_thread_id=topic_id, document=url, caption=caption)
    except TelegramBadRequest:
        raise


def validate_external_answer(question: TicketQuestion, incoming: IncomingMessage) -> tuple[dict[str, Any], str | None]:
    text = (incoming.text or "").strip()
    attachments = incoming.attachments
    expected = question.answer_type or ANSWER_TYPE_ANY
    if expected == ANSWER_TYPE_TEXT:
        if not text:
            return {}, "Пожалуйста, отправьте текст."
        return build_text_answer(question, text), None
    if expected == ANSWER_TYPE_MEDIA:
        if not attachments:
            return {}, "Пожалуйста, прикрепите файл."
        return build_media_answer(question, attachments), None
    if attachments:
        return build_media_answer(question, attachments), None
    if text:
        return build_text_answer(question, text), None
    return {}, "Отправьте текст или вложение."


def build_text_answer(question: TicketQuestion, text: str) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": ANSWER_TYPE_TEXT,
        "answer_text": text,
        "skipped": False,
    }


def build_media_answer(question: TicketQuestion, attachments: list[Attachment]) -> dict[str, Any]:
    media_files = [attachment_to_media_item(attachment, index) for index, attachment in enumerate(attachments)]
    answer = {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": ANSWER_TYPE_MEDIA,
        "answer_text": None,
        "skipped": False,
        "media_files": media_files,
    }
    if not media_files:
        return answer
    first = answer["media_files"][0]
    answer.update(
        {
            "file_id": first.get("file_id"),
            "file_url": first.get("file_url"),
            "media_type": first.get("media_type"),
            "caption": first.get("caption"),
        }
    )
    return answer


def append_external_media(answers: list[dict[str, Any]], question: TicketQuestion, attachments: list[Attachment]) -> list[dict[str, Any]]:
    answer = find_answer(answers, question.id)
    if answer is None:
        answer = {
            "question_id": question.id,
            "question_text": question.text,
            "answer_type": ANSWER_TYPE_MEDIA,
            "answer_text": None,
            "skipped": False,
            "media_files": [],
        }
        answers.append(answer)
    media_files = answer.setdefault("media_files", [])
    current_count = len(media_files)
    limit = max(1, question.max_files)
    for index, attachment in enumerate(attachments, start=current_count):
        if len(media_files) >= limit:
            break
        media_files.append(attachment_to_media_item(attachment, index))
    if media_files:
        first = media_files[0]
        answer["file_id"] = first.get("file_id")
        answer["file_url"] = first.get("file_url")
        answer["media_type"] = first.get("media_type")
        answer["caption"] = first.get("caption")
    return answers


def attachment_to_media_item(attachment: Attachment, sort_order: int) -> dict[str, Any]:
    file_id = attachment.file_id or attachment.file_url
    return {
        "file_id": file_id[:512] if file_id else None,
        "file_url": attachment.file_url,
        "media_type": attachment.type,
        "caption": attachment.caption,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "source_message_id": None,
        "sort_order": sort_order,
    }


def build_skipped_answer(question: TicketQuestion) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": question.answer_type or ANSWER_TYPE_ANY,
        "answer_text": None,
        "skipped": True,
    }


def find_answer(answers: list[dict[str, Any]], question_id: str) -> dict[str, Any] | None:
    return next((answer for answer in answers if answer.get("question_id") == question_id), None)


def build_question_text(question: TicketQuestion) -> str:
    if not question.help_text:
        return question.text
    return f"{question.text}\n\nПодсказка: {question.help_text}"
