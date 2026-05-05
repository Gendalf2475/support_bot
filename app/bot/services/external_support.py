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
from app.bot.constants.actions import (
    ACTION_CANCEL,
    ACTION_MEDIA_CONTINUE,
    ACTION_MINECRAFT_LOOKUP_CONTINUE,
    ACTION_MINECRAFT_LOOKUP_OTHER,
    ACTION_PROFILE_NICKNAME_CHANGE_CANCEL,
    ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM,
    ACTION_PROFILE_NICKNAME_OTHER,
    ACTION_PROFILE_NICKNAME_YES,
    ACTION_RESTART,
    ACTION_SKIP,
    ACTION_SUBMIT,
    EXTERNAL_CONTROL_TEXTS,
    TEXT_CANCEL,
    TEXT_CONTINUE,
    TEXT_LOOKUP_OTHER,
    TEXT_PROFILE_CHANGE_CONFIRM,
    TEXT_PROFILE_OTHER,
    TEXT_RESTART,
    TEXT_SKIP,
    TEXT_SUBMIT,
    TEXT_YES,
)
from app.bot.database.models import MessageDirection, Ticket, User
from app.bot.keyboards import support_close_ticket_keyboard
from app.bot.services.message_service import MessageService, TopicUnavailableError
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.ticket_form_service import (
    ANSWER_TYPE_ANY,
    ANSWER_TYPE_MEDIA,
    ANSWER_TYPE_TEXT,
    TicketForm,
    TicketFormService,
    TicketQuestion,
    get_minecraft_profile_field,
    is_minecraft_nickname_question,
    is_minecraft_profile_question,
    validate_profile_text_answer,
)
from app.bot.services.minecraft_service import MinecraftService, player_lookup_to_dict
from app.bot.services.ticket_formatter import TicketFormatter
from app.bot.services.ticket_service import TicketService
from app.bot.services.topic_service import TopicCreationError, TopicService
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)

OPEN_TICKET_EXISTS_TEXT = "У вас уже есть открытый тикет. Просто напишите сообщение сюда, и поддержка его увидит."
STALE_ACTION_TEXT = "Это действие уже неактуально."
CANCELLED_TEXT = "Заполнение тикета отменено."
FORMS_DISABLED_TEXT = "Система форм тикетов сейчас отключена. Попробуйте позже."


@dataclass
class ExternalDraft:
    form: TicketForm
    question_index: int = 0
    answers: list[dict[str, Any]] = field(default_factory=list)
    confirming: bool = False
    awaiting_profile_choice: bool = False
    awaiting_profile_change_confirmation: bool = False
    awaiting_minecraft_lookup_confirmation: bool = False
    pending_minecraft_answer: dict[str, Any] | None = None


class ExternalSupportProcessor:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        ticket_form_service: TicketFormService,
        platform_router: PlatformRouter,
        minecraft_service: MinecraftService,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.ticket_form_service = ticket_form_service
        self.platform_router = platform_router
        self.minecraft_service = minecraft_service
        self.drafts: dict[tuple[str, str], ExternalDraft] = {}
        self.platform_router.register_state_clearer(self.clear_user_state)
        self.platform_router.register_state_reader(self.get_user_state)

    def clear_user_state(self, platform: str, platform_user_id: str) -> None:
        self.drafts.pop((platform, str(platform_user_id)), None)

    def get_user_state(self, platform: str, platform_user_id: str) -> dict[str, Any] | None:
        draft = self.drafts.get((platform, str(platform_user_id)))
        if draft is None:
            return None
        question_id = None
        media_count = 0
        if draft.question_index < len(draft.form.questions):
            question = draft.form.questions[draft.question_index]
            question_id = question.id
            answer = find_answer(draft.answers, question.id)
            media_count = len(TicketService.extract_media_files(answer or {}))
        return {
            "state": get_external_draft_state(draft),
            "selected_form_id": draft.form.id,
            "current_question_index": draft.question_index,
            "current_question_id": question_id,
            "answers_count": len(draft.answers),
            "media_count": media_count,
            "pending_action": get_external_pending_action(draft),
            "confirming": draft.confirming,
        }

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
                logger.info("Active ticket found platform=%s platform_user_id=%s ticket_id=%s status=%s", incoming.platform, incoming.platform_user_id, open_ticket.id, open_ticket.status.value)
                text = (incoming.text or "").strip()
                if self.resolve_form(text) is not None:
                    await self.platform_router.send_text(user, OPEN_TICKET_EXISTS_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                if is_external_control_text(text):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                await self.forward_open_ticket_message(session, user, open_ticket, incoming)
                await ticket_service.mark_user_activity(open_ticket)
                await session.commit()
                return

            await self.handle_ticket_flow(session, user, incoming, key)
            await session.commit()

    async def handle_platform_form_selection(
        self,
        platform: str,
        platform_user_id: str,
        form_id: str,
        *,
        username: str | None = None,
        full_name: str | None = None,
    ) -> None:
        key = (platform, platform_user_id)
        async with self.sessionmaker() as session:
            user_result = await UserService(session).upsert_user(
                platform=platform,
                platform_user_id=platform_user_id,
                username=username,
                full_name=full_name,
            )
            user = user_result.user
            if user_result.changed and user.topic_id:
                await TopicService(UserService(session), self.settings.support_chat_id).sync_topic_title(self.bot, user)

            if user.blocked:
                await self.platform_router.send_text(user, "Вы заблокированы службой поддержки.", telegram_bot=self.bot)
                await session.commit()
                return

            ticket_service = TicketService(session, self.settings.support_chat_id)
            open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
            if open_ticket is not None:
                logger.info("Selected form_id=%s platform=%s platform_user_id=%s active_ticket_found=%s", form_id, platform, platform_user_id, True)
                await self.platform_router.send_text(user, OPEN_TICKET_EXISTS_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            if not self.ticket_form_service.enabled:
                await self.platform_router.send_text(user, FORMS_DISABLED_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            form = self.ticket_form_service.get_form(form_id)
            if form is None:
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            logger.info("Selected form_id=%s platform=%s platform_user_id=%s active_ticket_found=%s", form.id, platform, platform_user_id, False)
            self.drafts[key] = ExternalDraft(form=form)
            await self.send_question_or_profile_offer(session, user, self.drafts[key])
            await session.commit()

    async def handle_platform_action(
        self,
        platform: str,
        platform_user_id: str,
        action: str,
        *,
        username: str | None = None,
        full_name: str | None = None,
        question_index: int | None = None,
        form_id: str | None = None,
    ) -> None:
        key = (platform, platform_user_id)
        async with self.sessionmaker() as session:
            user_result = await UserService(session).upsert_user(
                platform=platform,
                platform_user_id=platform_user_id,
                username=username,
                full_name=full_name,
            )
            user = user_result.user
            if user_result.changed and user.topic_id:
                await TopicService(UserService(session), self.settings.support_chat_id).sync_topic_title(self.bot, user)

            if user.blocked:
                await self.platform_router.send_text(user, "Вы заблокированы службой поддержки.", telegram_bot=self.bot)
                await session.commit()
                return

            ticket_service = TicketService(session, self.settings.support_chat_id)
            open_ticket = await ticket_service.get_open_ticket_by_user_id(user.id)
            if open_ticket is not None:
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            draft = self.drafts.get(key)
            if draft is None:
                logger.info(
                    "Stale external action platform=%s platform_user_id=%s action=%s reason=missing_draft",
                    platform,
                    platform_user_id,
                    action,
                )
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return
            logger.info(
                "External action platform=%s platform_user_id=%s action=%s current_state=%s form_id=%s question_index=%s callback_question_index=%s form_id_hint=%s",
                platform,
                platform_user_id,
                action,
                get_external_draft_state(draft),
                draft.form.id,
                draft.question_index,
                question_index,
                form_id,
            )
            if form_id is not None and draft.form.id != form_id:
                logger.info(
                    "Stale external action platform=%s platform_user_id=%s action=%s reason=form_mismatch draft_form_id=%s callback_form_id=%s",
                    platform,
                    platform_user_id,
                    action,
                    draft.form.id,
                    form_id,
                )
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            if action == ACTION_CANCEL:
                if form_id is not None and not draft.confirming:
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                if question_index is not None and (draft.confirming or question_index != draft.question_index):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                self.drafts.pop(key, None)
                await self.platform_router.send_form_menu(
                    user,
                    self.ticket_form_service.get_forms(),
                    text=CANCELLED_TEXT,
                    telegram_bot=self.bot,
                )
                await session.commit()
                return

            if action == ACTION_RESTART:
                if not draft.confirming:
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                self.drafts[key] = ExternalDraft(form=draft.form)
                await self.send_question_or_profile_offer(session, user, self.drafts[key])
                await session.commit()
                return

            if action == ACTION_SUBMIT:
                if not draft.confirming:
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                ticket = await self.submit_ticket(session, user, draft)
                self.drafts.pop(key, None)
                await self.platform_router.send_ticket_sent(
                    user,
                    ticket.id,
                    success_text=TicketService.build_success_text(draft.form, ticket, user),
                    telegram_bot=self.bot,
                )
                await session.commit()
                return

            if action in {ACTION_MINECRAFT_LOOKUP_CONTINUE, ACTION_MINECRAFT_LOOKUP_OTHER}:
                if (
                    draft.confirming
                    or not draft.awaiting_minecraft_lookup_confirmation
                    or draft.question_index >= len(draft.form.questions)
                    or (question_index is not None and question_index != draft.question_index)
                ):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return

                if action == ACTION_MINECRAFT_LOOKUP_CONTINUE:
                    await self.accept_pending_minecraft_lookup_answer(session, user, draft)
                    await session.commit()
                    return

                await self.return_to_minecraft_question(user, draft)
                await session.commit()
                return

            if action in {ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM, ACTION_PROFILE_NICKNAME_CHANGE_CANCEL}:
                if (
                    draft.confirming
                    or not draft.awaiting_profile_change_confirmation
                    or draft.question_index >= len(draft.form.questions)
                    or (question_index is not None and question_index != draft.question_index)
                ):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return

                if action == ACTION_PROFILE_NICKNAME_CHANGE_CONFIRM:
                    draft.awaiting_profile_change_confirmation = False
                    draft.awaiting_profile_choice = False
                    await self.platform_router.send_question(
                        user,
                        draft.form,
                        draft.question_index,
                        prefix_text="Введите новый игровой ник:",
                        telegram_bot=self.bot,
                    )
                    await session.commit()
                    return

                draft.awaiting_profile_change_confirmation = False
                draft.awaiting_profile_choice = False
                await self.apply_saved_minecraft_nickname(session, user, draft)
                await session.commit()
                return

            if action in {ACTION_PROFILE_NICKNAME_YES, ACTION_PROFILE_NICKNAME_OTHER}:
                if (
                    draft.confirming
                    or not draft.awaiting_profile_choice
                    or draft.question_index >= len(draft.form.questions)
                    or (question_index is not None and question_index != draft.question_index)
                ):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return

                question = draft.form.questions[draft.question_index]
                if not is_minecraft_nickname_question(question):
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return

                if action == ACTION_PROFILE_NICKNAME_YES:
                    nickname = str(user.minecraft_nickname or "").strip()
                    if not nickname:
                        await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                        await session.commit()
                        return
                    validation_error = validate_profile_text_answer(question, nickname)
                    if validation_error:
                        draft.awaiting_profile_choice = False
                        await self.platform_router.send_question(
                            user,
                            draft.form,
                            draft.question_index,
                            prefix_text=validation_error,
                            telegram_bot=self.bot,
                        )
                        await session.commit()
                        return
                    draft.awaiting_profile_choice = False
                    answer = build_text_answer(question, nickname)
                    if await self.maybe_handle_minecraft_lookup(session, user, draft, question, answer):
                        await session.commit()
                        return
                    draft.answers.append(answer)
                    await maybe_save_minecraft_nickname(session, user, question, answer)
                    await self.advance_or_summary(session, user, draft)
                    await session.commit()
                    return

                await self.begin_minecraft_nickname_change(user, draft)
                await session.commit()
                return

            if draft.confirming or draft.question_index >= len(draft.form.questions):
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return
            if question_index is not None and question_index != draft.question_index:
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                await session.commit()
                return

            question = draft.form.questions[draft.question_index]
            if action == ACTION_SKIP:
                if question.required:
                    await self.platform_router.send_question(
                        user,
                        draft.form,
                        draft.question_index,
                        prefix_text="Это обязательный вопрос.",
                        telegram_bot=self.bot,
                    )
                    await session.commit()
                    return
                draft.answers.append(build_skipped_answer(question))
                await self.advance_or_summary(session, user, draft)
                await session.commit()
                return

            if action == ACTION_MEDIA_CONTINUE:
                if not question_accepts_multiple_media(question):
                    logger.info(
                        "Rejected external media_continue platform=%s platform_user_id=%s form_id=%s question_id=%s question_index=%s reason=not_media_question",
                        platform,
                        platform_user_id,
                        draft.form.id,
                        question.id,
                        draft.question_index,
                    )
                    await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                    await session.commit()
                    return
                answer = find_answer(draft.answers, question.id)
                media_count = len(TicketService.extract_media_files(answer or {}))
                logger.info(
                    "External media_continue platform=%s platform_user_id=%s form_id=%s question_id=%s question_index=%s media_count=%s required=%s allow_multiple=%s",
                    platform,
                    platform_user_id,
                    draft.form.id,
                    question.id,
                    draft.question_index,
                    media_count,
                    question.required,
                    question.allow_multiple,
                )
                if media_count == 0 and question.required:
                    await self.platform_router.send_question(
                        user,
                        draft.form,
                        draft.question_index,
                        prefix_text="Пожалуйста, прикрепите хотя бы один файл.",
                        telegram_bot=self.bot,
                    )
                    await session.commit()
                    return
                if media_count == 0:
                    draft.answers.append(build_skipped_answer(question))
                await self.advance_or_summary(session, user, draft)
                await session.commit()
                return

            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
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
            if is_external_control_text(text):
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                return
            if not self.ticket_form_service.enabled:
                await self.platform_router.send_text(user, FORMS_DISABLED_TEXT, telegram_bot=self.bot)
                return
            form = self.resolve_form(text)
            if form is None:
                await self.platform_router.send_form_menu(user, self.ticket_form_service.get_forms(), telegram_bot=self.bot)
                return
            draft = ExternalDraft(form=form)
            self.drafts[key] = draft
            logger.info("Selected form_id=%s platform=%s platform_user_id=%s active_ticket_found=%s", form.id, user.platform, user.platform_user_id, False)
            await self.send_question_or_profile_offer(session, user, draft)
            return

        if text.casefold() in TEXT_CANCEL:
            self.drafts.pop(key, None)
            await self.platform_router.send_form_menu(
                user,
                self.ticket_form_service.get_forms(),
                text=CANCELLED_TEXT,
                telegram_bot=self.bot,
            )
            return

        selected_form = self.resolve_form(text)
        if selected_form is not None:
            logger.info("Selected form_id=%s platform=%s platform_user_id=%s active_ticket_found=%s", selected_form.id, user.platform, user.platform_user_id, False)
            self.drafts[key] = ExternalDraft(form=selected_form)
            await self.send_question_or_profile_offer(session, user, self.drafts[key])
            return

        if draft.awaiting_profile_choice:
            await self.handle_profile_choice_text(session, user, incoming, draft)
            return

        if draft.awaiting_profile_change_confirmation:
            await self.handle_profile_change_confirmation_text(session, user, incoming, draft)
            return

        if draft.awaiting_minecraft_lookup_confirmation:
            await self.handle_minecraft_lookup_confirmation_text(session, user, incoming, draft)
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
        raw_text = (incoming.text or "").strip()
        text = raw_text.casefold()
        if text in TEXT_SUBMIT:
            ticket = await self.submit_ticket(session, user, draft)
            self.drafts.pop(key, None)
            await self.platform_router.send_ticket_sent(
                user,
                ticket.id,
                success_text=TicketService.build_success_text(draft.form, ticket, user),
                telegram_bot=self.bot,
            )
            return

        if text in TEXT_RESTART:
            self.drafts[key] = ExternalDraft(form=draft.form)
            await self.send_question_or_profile_offer(session, user, self.drafts[key])
            return

        if text in TEXT_CANCEL:
            self.drafts.pop(key, None)
            await self.platform_router.send_form_menu(
                user,
                self.ticket_form_service.get_forms(),
                text=CANCELLED_TEXT,
                telegram_bot=self.bot,
            )
            return

        if is_external_control_text(raw_text):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
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
        normalized_text = text.casefold()
        if normalized_text in TEXT_SKIP:
            if question.required:
                await self.platform_router.send_question(
                    user,
                    draft.form,
                    draft.question_index,
                    prefix_text="Это обязательный вопрос.",
                    telegram_bot=self.bot,
                )
                return
            draft.answers.append(build_skipped_answer(question))
            await self.advance_or_summary(session, user, draft)
            return
        if normalized_text in TEXT_SUBMIT | TEXT_RESTART:
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return
        if normalized_text in TEXT_CONTINUE and not question_accepts_multiple_media(question):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return

        if question_accepts_multiple_media(question):
            answer = find_answer(draft.answers, question.id)
            if normalized_text in TEXT_CONTINUE:
                media_count = len(TicketService.extract_media_files(answer or {}))
                if media_count == 0 and question.required:
                    await self.platform_router.send_question(
                        user,
                        draft.form,
                        draft.question_index,
                        prefix_text="Пожалуйста, прикрепите хотя бы один файл.",
                        telegram_bot=self.bot,
                    )
                    return
                if media_count == 0:
                    draft.answers.append(build_skipped_answer(question))
                await self.advance_or_summary(session, user, draft)
                return

            if incoming.attachments:
                draft.answers = append_external_media(draft.answers, question, incoming.attachments)
                media_count = len(TicketService.extract_media_files(find_answer(draft.answers, question.id) or {}))
                max_files = question.max_files
                limit_reached = max_files is not None and media_count >= max_files
                logger.info(
                    "External media added user_id=%s platform=%s platform_user_id=%s form_id=%s question_id=%s question_index=%s media_count=%s attachments_in_message=%s",
                    user.id,
                    user.platform,
                    user.platform_user_id,
                    draft.form.id,
                    question.id,
                    draft.question_index,
                    media_count,
                    len(incoming.attachments),
                )
                await self.platform_router.send_media_continue(
                    user,
                    draft.question_index,
                    media_count,
                    max_files,
                    limit_reached=limit_reached,
                    telegram_bot=self.bot,
                )
                return

            if len(TicketService.extract_media_files(answer or {})) > 0:
                await self.platform_router.send_media_continue(
                    user,
                    draft.question_index,
                    len(TicketService.extract_media_files(answer or {})),
                    question.max_files,
                    limit_reached=question.max_files is not None
                    and len(TicketService.extract_media_files(answer or {})) >= question.max_files,
                    telegram_bot=self.bot,
                )
                return

        answer, error = validate_external_answer(question, incoming)
        if error:
            await self.platform_router.send_question(
                user,
                draft.form,
                draft.question_index,
                prefix_text=error,
                telegram_bot=self.bot,
            )
            return

        if await self.maybe_handle_minecraft_lookup(session, user, draft, question, answer):
            return

        draft.answers.append(answer)
        await maybe_save_minecraft_nickname(session, user, question, answer)
        await self.advance_or_summary(session, user, draft)

    async def advance_or_summary(self, session: AsyncSession, user: User, draft: ExternalDraft) -> None:
        draft.question_index += 1
        if draft.question_index < len(draft.form.questions):
            await self.send_question_or_profile_offer(session, user, draft)
            return
        await self.show_summary(user, draft)

    async def send_question_or_profile_offer(self, session: AsyncSession, user: User, draft: ExternalDraft) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.show_summary(user, draft)
            return

        question = draft.form.questions[draft.question_index]
        if should_auto_apply_minecraft_nickname(self.settings, user, question):
            nickname = str(user.minecraft_nickname or "").strip()
            validation_error = validate_profile_text_answer(question, nickname)
            if validation_error:
                await self.platform_router.send_question(
                    user,
                    draft.form,
                    draft.question_index,
                    prefix_text=validation_error,
                    telegram_bot=self.bot,
                )
                return
            answer = build_text_answer(question, nickname)
            if await self.maybe_handle_minecraft_lookup(session, user, draft, question, answer):
                return
            draft.answers.append(answer)
            await maybe_save_minecraft_nickname(session, user, question, answer)
            await self.advance_or_summary(session, user, draft)
            return

        if should_offer_minecraft_nickname(self.settings, user, question):
            draft.awaiting_profile_choice = True
            draft.awaiting_profile_change_confirmation = False
            draft.awaiting_minecraft_lookup_confirmation = False
            draft.pending_minecraft_answer = None
            await self.platform_router.send_minecraft_nickname_offer(
                user,
                draft.question_index,
                str(user.minecraft_nickname),
                change_label=get_profile_change_label(self.settings, question),
                text=build_profile_offer_text(self.settings, user, question),
                telegram_bot=self.bot,
            )
            return

        draft.awaiting_profile_choice = False
        draft.awaiting_profile_change_confirmation = False
        draft.awaiting_minecraft_lookup_confirmation = False
        draft.pending_minecraft_answer = None
        logger.info(
            "Sending external question user_id=%s platform=%s platform_user_id=%s form_id=%s question_id=%s question_index=%s answer_type=%s allow_multiple=%s max_files=%s state=%s",
            user.id,
            user.platform,
            user.platform_user_id,
            draft.form.id,
            question.id,
            draft.question_index,
            question.answer_type,
            question.allow_multiple,
            question.max_files,
            get_external_draft_state(draft),
        )
        await self.platform_router.send_question(user, draft.form, draft.question_index, telegram_bot=self.bot)

    async def handle_profile_choice_text(self, session: AsyncSession, user: User, incoming: IncomingMessage, draft: ExternalDraft) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return

        question = draft.form.questions[draft.question_index]
        text = (incoming.text or "").strip().casefold()
        if text in TEXT_YES:
            nickname = str(user.minecraft_nickname or "").strip()
            if not nickname or not is_minecraft_nickname_question(question):
                await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
                return
            validation_error = validate_profile_text_answer(question, nickname)
            if validation_error:
                draft.awaiting_profile_choice = False
                await self.platform_router.send_question(
                    user,
                    draft.form,
                    draft.question_index,
                    prefix_text=validation_error,
                    telegram_bot=self.bot,
                )
                return
            draft.awaiting_profile_choice = False
            answer = build_text_answer(question, nickname)
            if await self.maybe_handle_minecraft_lookup(session, user, draft, question, answer):
                return
            draft.answers.append(answer)
            await maybe_save_minecraft_nickname(session, user, question, answer)
            await self.advance_or_summary(session, user, draft)
            return

        if text in TEXT_PROFILE_OTHER:
            await self.begin_minecraft_nickname_change(user, draft)
            return

        await self.platform_router.send_minecraft_nickname_offer(
            user,
            draft.question_index,
            str(user.minecraft_nickname or ""),
            change_label=get_profile_change_label(self.settings, question),
            text=build_profile_offer_text(self.settings, user, question),
            telegram_bot=self.bot,
        )

    async def begin_minecraft_nickname_change(self, user: User, draft: ExternalDraft) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return

        question = draft.form.questions[draft.question_index]
        if not is_minecraft_nickname_question(question):
            draft.awaiting_profile_choice = False
            await self.platform_router.send_question(user, draft.form, draft.question_index, telegram_bot=self.bot)
            return

        cooldown_text = get_minecraft_nickname_cooldown_text(self.settings, user)
        if cooldown_text:
            await self.platform_router.send_minecraft_nickname_offer(
                user,
                draft.question_index,
                str(user.minecraft_nickname or ""),
                change_label=get_profile_change_label(self.settings, question),
                text=cooldown_text,
                telegram_bot=self.bot,
            )
            return

        if self.settings.minecraft_nickname_required_enabled and self.settings.minecraft_nickname_lock_enabled and str(user.minecraft_nickname or "").strip():
            draft.awaiting_profile_choice = False
            draft.awaiting_profile_change_confirmation = True
            await self.platform_router.send_minecraft_nickname_change_confirmation(
                user,
                draft.question_index,
                str(user.minecraft_nickname or ""),
                telegram_bot=self.bot,
            )
            return

        draft.awaiting_profile_choice = False
        draft.awaiting_profile_change_confirmation = False
        await self.platform_router.send_question(
            user,
            draft.form,
            draft.question_index,
            prefix_text="Введите новый игровой ник:",
            telegram_bot=self.bot,
        )

    async def apply_saved_minecraft_nickname(self, session: AsyncSession, user: User, draft: ExternalDraft) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return
        question = draft.form.questions[draft.question_index]
        nickname = str(user.minecraft_nickname or "").strip()
        if not nickname or not is_minecraft_nickname_question(question):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return
        validation_error = validate_profile_text_answer(question, nickname)
        if validation_error:
            await self.platform_router.send_question(
                user,
                draft.form,
                draft.question_index,
                prefix_text=validation_error,
                telegram_bot=self.bot,
            )
            return
        answer = build_text_answer(question, nickname)
        if await self.maybe_handle_minecraft_lookup(session, user, draft, question, answer):
            return
        draft.answers.append(answer)
        await maybe_save_minecraft_nickname(session, user, question, answer)
        await self.advance_or_summary(session, user, draft)

    async def maybe_handle_minecraft_lookup(
        self,
        session: AsyncSession,
        user: User,
        draft: ExternalDraft,
        question: TicketQuestion,
        answer: dict[str, Any],
    ) -> bool:
        profile_field = get_minecraft_profile_field(question)
        if profile_field is None:
            return False

        nickname = str(answer.get("answer_text") or "").strip()
        if not nickname:
            return False

        lookup = await self.minecraft_service.check_player(nickname)
        answer["profile_field"] = profile_field
        answer["minecraft_lookup"] = player_lookup_to_dict(lookup)

        if lookup.error == "disabled":
            return False

        if lookup.exists is True:
            await self.platform_router.send_text(user, f"✅ Игрок найден: {lookup.nickname or nickname}", telegram_bot=self.bot)
            return False

        if lookup.exists is False:
            if self.settings.minecraft_nickname_check_strict:
                await self.platform_router.send_question(
                    user,
                    draft.form,
                    draft.question_index,
                    prefix_text=(
                        f"❌ Игрок с ником {nickname} не найден на сервере.\n"
                        "Проверьте ник и введите снова."
                    ),
                    telegram_bot=self.bot,
                )
                return True

            draft.awaiting_minecraft_lookup_confirmation = True
            draft.pending_minecraft_answer = answer
            await self.platform_router.send_minecraft_lookup_confirmation(
                user,
                draft.question_index,
                nickname,
                telegram_bot=self.bot,
            )
            return True

        if self.settings.minecraft_nickname_check_strict:
            await self.platform_router.send_question(
                user,
                draft.form,
                draft.question_index,
                prefix_text=(
                    "⚠️ Сейчас не удалось проверить ник через сервер.\n"
                    "Попробуйте позже или введите ник ещё раз."
                ),
                telegram_bot=self.bot,
            )
            return True

        return False

    async def accept_pending_minecraft_lookup_answer(
        self,
        session: AsyncSession,
        user: User,
        draft: ExternalDraft,
    ) -> None:
        if draft.pending_minecraft_answer is None or draft.question_index >= len(draft.form.questions):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return

        question = draft.form.questions[draft.question_index]
        answer = draft.pending_minecraft_answer
        draft.awaiting_minecraft_lookup_confirmation = False
        draft.pending_minecraft_answer = None
        draft.answers.append(answer)
        await maybe_save_minecraft_nickname(session, user, question, answer)
        await self.advance_or_summary(session, user, draft)

    async def return_to_minecraft_question(self, user: User, draft: ExternalDraft) -> None:
        if draft.question_index >= len(draft.form.questions):
            await self.platform_router.send_text(user, STALE_ACTION_TEXT, telegram_bot=self.bot)
            return
        draft.awaiting_minecraft_lookup_confirmation = False
        draft.pending_minecraft_answer = None
        await self.platform_router.send_question(user, draft.form, draft.question_index, telegram_bot=self.bot)

    async def handle_minecraft_lookup_confirmation_text(
        self,
        session: AsyncSession,
        user: User,
        incoming: IncomingMessage,
        draft: ExternalDraft,
    ) -> None:
        text = (incoming.text or "").strip().casefold()
        if text in TEXT_CONTINUE:
            await self.accept_pending_minecraft_lookup_answer(session, user, draft)
            return
        if text in TEXT_LOOKUP_OTHER:
            await self.return_to_minecraft_question(user, draft)
            return
        await self.platform_router.send_text(user, "Напишите: Продолжить или Ввести другой.", telegram_bot=self.bot)

    async def handle_profile_change_confirmation_text(
        self,
        session: AsyncSession,
        user: User,
        incoming: IncomingMessage,
        draft: ExternalDraft,
    ) -> None:
        text = (incoming.text or "").strip().casefold()
        if text in TEXT_PROFILE_CHANGE_CONFIRM:
            draft.awaiting_profile_change_confirmation = False
            await self.platform_router.send_question(
                user,
                draft.form,
                draft.question_index,
                prefix_text="Введите новый игровой ник:",
                telegram_bot=self.bot,
            )
            return
        if text in TEXT_CANCEL:
            draft.awaiting_profile_change_confirmation = False
            await self.apply_saved_minecraft_nickname(session, user, draft)
            return
        await self.platform_router.send_minecraft_nickname_change_confirmation(
            user,
            draft.question_index,
            str(user.minecraft_nickname or ""),
            telegram_bot=self.bot,
        )

    async def show_summary(self, user: User, draft: ExternalDraft) -> None:
        draft.confirming = True
        draft.awaiting_profile_choice = False
        draft.awaiting_profile_change_confirmation = False
        draft.awaiting_minecraft_lookup_confirmation = False
        draft.pending_minecraft_answer = None
        await self.platform_router.send_ticket_preview(user, draft.form, draft.answers, telegram_bot=self.bot)

    async def submit_ticket(self, session: AsyncSession, user: User, draft: ExternalDraft) -> Ticket:
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
        return ticket

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
    if is_minecraft_profile_question(question):
        if not text:
            return {}, "Пожалуйста, отправьте текст."
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
        return build_text_answer(question, text), None

    expected = question.answer_type or ANSWER_TYPE_ANY
    if expected == ANSWER_TYPE_TEXT:
        if not text:
            return {}, "Пожалуйста, отправьте текст."
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
        return build_text_answer(question, text), None
    if expected == ANSWER_TYPE_MEDIA:
        if not attachments:
            return {}, "Пожалуйста, прикрепите файл."
        return build_media_answer(question, attachments), None
    if attachments:
        return build_media_answer(question, attachments), None
    if text:
        validation_error = validate_profile_text_answer(question, text)
        if validation_error:
            return {}, validation_error
        return build_text_answer(question, text), None
    return {}, "Отправьте текст или вложение."


def should_offer_minecraft_nickname(settings: Settings, user: User, question: TicketQuestion) -> bool:
    if not is_minecraft_nickname_question(question) or not str(user.minecraft_nickname or "").strip():
        return False
    if settings.minecraft_nickname_required_enabled:
        return settings.minecraft_nickname_confirm_each_ticket
    return (
        settings.minecraft_nickname_autofill_enabled
    )


def should_auto_apply_minecraft_nickname(settings: Settings, user: User, question: TicketQuestion) -> bool:
    return (
        settings.minecraft_nickname_required_enabled
        and not settings.minecraft_nickname_confirm_each_ticket
        and is_minecraft_nickname_question(question)
        and bool(str(user.minecraft_nickname or "").strip())
    )


def get_profile_change_label(settings: Settings, question: TicketQuestion | None) -> str:
    if question is not None and settings.minecraft_nickname_required_enabled and is_minecraft_nickname_question(question):
        return "Изменить ник"
    return "Ввести другой"


def build_profile_offer_text(settings: Settings, user: User, question: TicketQuestion) -> str:
    nickname = str(user.minecraft_nickname or "").strip()
    if settings.minecraft_nickname_required_enabled and is_minecraft_nickname_question(question):
        return f"Ваш игровой ник: {nickname}?"
    return f"Использовать прошлый ник {nickname}?"


def get_minecraft_nickname_cooldown_text(settings: Settings, user: User) -> str | None:
    from datetime import timedelta
    import time

    cooldown_hours = max(0, settings.minecraft_nickname_change_cooldown_hours)
    if cooldown_hours <= 0 or user.minecraft_nickname_updated_at is None:
        return None
    available_at = user.minecraft_nickname_updated_at + timedelta(hours=cooldown_hours)
    if time.time() >= available_at.timestamp():
        return None
    return "Игровой ник можно будет изменить позже."


async def maybe_save_minecraft_nickname(
    session: AsyncSession,
    user: User,
    question: TicketQuestion,
    answer: dict[str, Any],
) -> None:
    if not is_minecraft_nickname_question(question):
        return
    if answer.get("skipped") or answer.get("answer_type") != ANSWER_TYPE_TEXT:
        return
    nickname = str(answer.get("answer_text") or "").strip()
    if not nickname or user.minecraft_nickname == nickname:
        return
    await UserService(session).set_minecraft_nickname(user, nickname)


def build_text_answer(question: TicketQuestion, text: str) -> dict[str, Any]:
    return {
        "question_id": question.id,
        "question_text": question.text,
        "answer_type": ANSWER_TYPE_TEXT,
        "answer_text": text,
        "profile_field": get_minecraft_profile_field(question),
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
    limit = question.max_files
    for index, attachment in enumerate(attachments, start=current_count):
        if limit is not None and len(media_files) >= limit:
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


def get_external_draft_state(draft: ExternalDraft) -> str:
    if draft.confirming:
        return "preview"
    if draft.awaiting_profile_choice:
        return "confirming_nickname"
    if draft.awaiting_profile_change_confirmation:
        return "confirming_nickname_change"
    if draft.awaiting_minecraft_lookup_confirmation:
        return "confirming_not_found_nickname"
    if draft.question_index < len(draft.form.questions):
        question = draft.form.questions[draft.question_index]
        if question_accepts_multiple_media(question) and find_answer(draft.answers, question.id) is not None:
            return "collecting_media"
    return "answering"


def get_external_pending_action(draft: ExternalDraft) -> str | None:
    if draft.awaiting_profile_choice:
        return "nickname_choice"
    if draft.awaiting_profile_change_confirmation:
        return "nickname_change_confirmation"
    if draft.awaiting_minecraft_lookup_confirmation:
        return "minecraft_lookup_confirmation"
    if draft.confirming:
        return "ticket_preview"
    if draft.question_index < len(draft.form.questions):
        question = draft.form.questions[draft.question_index]
        if question_accepts_multiple_media(question):
            return "media_continue"
    return None


def question_accepts_multiple_media(question: TicketQuestion) -> bool:
    return question.allow_multiple and question.answer_type in {ANSWER_TYPE_MEDIA, ANSWER_TYPE_ANY}


def is_external_control_text(text: str) -> bool:
    return (text or "").strip().casefold() in EXTERNAL_CONTROL_TEXTS


def build_question_text(question: TicketQuestion) -> str:
    if not question.help_text:
        return question.text
    return f"{question.text}\n\nПодсказка: {question.help_text}"
