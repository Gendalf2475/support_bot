from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.database.models import MessageDirection, MessageMap, Ticket, User, utcnow


logger = logging.getLogger(__name__)


class TopicUnavailableError(RuntimeError):
    pass


class MessageService:
    def __init__(self, session: AsyncSession, support_chat_id: int) -> None:
        self.session = session
        self.support_chat_id = support_chat_id

    async def copy_user_message_to_support(
        self,
        bot: Bot,
        message: Message,
        user: User,
        ticket: Ticket | None = None,
    ) -> int:
        if not user.topic_id:
            raise TopicUnavailableError("User has no topic_id")

        try:
            copy_result = await bot.copy_message(
                chat_id=self.support_chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=user.topic_id,
            )
            support_message_id = copy_result.message_id
        except TelegramBadRequest as error:
            if self.is_topic_unavailable_error(error):
                raise TopicUnavailableError(str(error)) from error
            logger.warning(
                "Failed to copy user message telegram_id=%s message_id=%s to topic_id=%s: %s",
                user.telegram_id,
                message.message_id,
                user.topic_id,
                error,
            )
            support_message_id = await self._send_user_message_fallback(bot, message, user, error)
        except TelegramAPIError as error:
            logger.error(
                "Telegram error while copying user message telegram_id=%s message_id=%s: %s",
                user.telegram_id,
                message.message_id,
                error,
            )
            support_message_id = await self._send_user_message_fallback(bot, message, user, error)

        await self.create_message_map(
            user=user,
            user_message_id=message.message_id,
            support_message_id=support_message_id,
            topic_id=user.topic_id,
            direction=MessageDirection.USER_TO_SUPPORT,
            ticket_id=ticket.id if ticket else None,
        )
        logger.info(
            "Forwarded user -> support telegram_id=%s user_message_id=%s support_message_id=%s topic_id=%s",
            user.telegram_id,
            message.message_id,
            support_message_id,
            user.topic_id,
        )
        return support_message_id

    async def copy_support_message_to_user(
        self,
        bot: Bot,
        message: Message,
        user: User,
        ticket: Ticket | None = None,
    ) -> int | None:
        try:
            copy_result = await bot.copy_message(
                chat_id=user.telegram_id,
                from_chat_id=self.support_chat_id,
                message_id=message.message_id,
            )
            user_message_id = copy_result.message_id
        except TelegramForbiddenError as error:
            await self.notify_delivery_error(bot, user, error)
            return None
        except TelegramBadRequest as error:
            if self.is_user_unavailable_error(error):
                await self.notify_delivery_error(bot, user, error)
                return None
            logger.warning(
                "Failed to copy support message topic_id=%s message_id=%s to telegram_id=%s: %s",
                message.message_thread_id,
                message.message_id,
                user.telegram_id,
                error,
            )
            user_message_id = await self._send_support_message_fallback(bot, message, user, error)
            if user_message_id is None:
                return None
        except TelegramAPIError as error:
            await self.notify_delivery_error(bot, user, error)
            return None

        await self.create_message_map(
            user=user,
            user_message_id=user_message_id,
            support_message_id=message.message_id,
            topic_id=user.topic_id or message.message_thread_id,
            direction=MessageDirection.SUPPORT_TO_USER,
            ticket_id=ticket.id if ticket else None,
        )
        logger.info(
            "Forwarded support -> user telegram_id=%s support_message_id=%s user_message_id=%s topic_id=%s",
            user.telegram_id,
            message.message_id,
            user_message_id,
            message.message_thread_id,
        )
        return user_message_id

    async def create_message_map(
        self,
        user: User,
        user_message_id: int,
        support_message_id: int,
        topic_id: int,
        direction: MessageDirection,
        ticket_id: int | None = None,
    ) -> MessageMap:
        message_map = MessageMap(
            user_id=user.id,
            ticket_id=ticket_id,
            user_message_id=user_message_id,
            support_message_id=support_message_id,
            topic_id=topic_id,
            direction=direction,
        )
        self.session.add(message_map)
        await self.session.flush()
        return message_map

    async def copy_ticket_form_media_to_support(
        self,
        bot: Bot,
        user: User,
        ticket: Ticket,
        source_message_id: int,
    ) -> int | None:
        if not ticket.topic_id:
            raise TopicUnavailableError("Ticket has no topic_id")

        try:
            copy_result = await bot.copy_message(
                chat_id=self.support_chat_id,
                from_chat_id=user.telegram_id,
                message_id=source_message_id,
                message_thread_id=ticket.topic_id,
            )
        except TelegramBadRequest as error:
            if self.is_topic_unavailable_error(error):
                raise TopicUnavailableError(str(error)) from error
            logger.error(
                "Failed to copy ticket form media ticket_id=%s telegram_id=%s message_id=%s: %s",
                ticket.id,
                user.telegram_id,
                source_message_id,
                error,
            )
            await self._notify_ticket_media_copy_error(bot, ticket.topic_id, source_message_id, error)
            return None
        except TelegramAPIError as error:
            logger.error(
                "Telegram error while copying ticket form media ticket_id=%s telegram_id=%s message_id=%s: %s",
                ticket.id,
                user.telegram_id,
                source_message_id,
                error,
            )
            await self._notify_ticket_media_copy_error(bot, ticket.topic_id, source_message_id, error)
            return None

        await self.create_message_map(
            user=user,
            user_message_id=source_message_id,
            support_message_id=copy_result.message_id,
            topic_id=ticket.topic_id,
            direction=MessageDirection.TICKET_FORM_MEDIA,
            ticket_id=ticket.id,
        )
        return copy_result.message_id

    async def find_user_to_support_map(self, user: User, user_message_id: int) -> MessageMap | None:
        statement = select(MessageMap).where(
            MessageMap.user_id == user.id,
            MessageMap.user_message_id == user_message_id,
            MessageMap.direction == MessageDirection.USER_TO_SUPPORT,
        )
        return await self.session.scalar(statement)

    async def find_support_to_user_map(self, topic_id: int, support_message_id: int) -> MessageMap | None:
        statement = select(MessageMap).where(
            MessageMap.topic_id == topic_id,
            MessageMap.support_message_id == support_message_id,
            MessageMap.direction == MessageDirection.SUPPORT_TO_USER,
        )
        return await self.session.scalar(statement)

    async def mark_edited(self, message_map: MessageMap) -> None:
        message_map.edited_at = utcnow()
        await self.session.flush()

    async def edit_user_message_copy_in_support(self, bot: Bot, message: Message, user: User) -> bool:
        text = self.extract_text_or_caption(message)
        if not text:
            return False

        message_map = await self.find_user_to_support_map(user, message.message_id)
        if message_map is None:
            await self._send_user_edit_fallback(bot, user, text, "Связь сообщений не найдена.")
            return False

        try:
            if message.text:
                await bot.edit_message_text(
                    chat_id=self.support_chat_id,
                    message_id=message_map.support_message_id,
                    text=self._limit_text(text),
                )
            else:
                await bot.edit_message_caption(
                    chat_id=self.support_chat_id,
                    message_id=message_map.support_message_id,
                    caption=text,
                )
        except TelegramAPIError as error:
            logger.error(
                "Failed to edit support copy for user message telegram_id=%s user_message_id=%s support_message_id=%s: %s",
                user.telegram_id,
                message.message_id,
                message_map.support_message_id,
                error,
            )
            await self._send_user_edit_fallback(bot, user, text, str(error))
            await self.mark_edited(message_map)
            return False

        await self.mark_edited(message_map)
        return True

    async def edit_support_message_copy_to_user(self, bot: Bot, message: Message, user: User) -> bool:
        text = self.extract_text_or_caption(message)
        if not text:
            return False

        if message.message_thread_id is None:
            return False

        message_map = await self.find_support_to_user_map(message.message_thread_id, message.message_id)
        if message_map is None:
            await self._send_support_edit_fallback(bot, user, text, "Связь сообщений не найдена.")
            return False

        try:
            if message.text:
                await bot.edit_message_text(
                    chat_id=user.telegram_id,
                    message_id=message_map.user_message_id,
                    text=self._limit_text(text),
                )
            else:
                await bot.edit_message_caption(
                    chat_id=user.telegram_id,
                    message_id=message_map.user_message_id,
                    caption=text,
                )
        except TelegramAPIError as error:
            logger.error(
                "Failed to edit user copy for support message telegram_id=%s support_message_id=%s user_message_id=%s: %s",
                user.telegram_id,
                message.message_id,
                message_map.user_message_id,
                error,
            )
            await self._send_support_edit_fallback(bot, user, text, str(error))
            await self.mark_edited(message_map)
            return False

        await self.mark_edited(message_map)
        return True

    async def _send_user_message_fallback(self, bot: Bot, message: Message, user: User, error: Exception) -> int:
        text = self.extract_text_or_caption(message)
        if text:
            fallback_text = (
                "Не удалось скопировать сообщение пользователя автоматически.\n\n"
                f"{text}\n\n"
                f"Тип сообщения: {message.content_type}\n"
                f"Ошибка: {error}"
            )
        else:
            fallback_text = (
                "Не удалось скопировать сообщение пользователя автоматически.\n\n"
                f"Тип сообщения: {message.content_type}\n"
                f"Ошибка: {error}"
            )

        try:
            sent = await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=user.topic_id,
                text=self._limit_text(fallback_text),
            )
        except TelegramBadRequest as send_error:
            if self.is_topic_unavailable_error(send_error):
                raise TopicUnavailableError(str(send_error)) from send_error
            raise
        return sent.message_id

    async def _send_support_message_fallback(
        self,
        bot: Bot,
        message: Message,
        user: User,
        error: Exception,
    ) -> int | None:
        text = self.extract_text_or_caption(message)
        if text:
            fallback_text = f"Сообщение поддержки:\n\n{text}"
        else:
            fallback_text = (
                "Поддержка отправила сообщение, но бот не смог скопировать его в ЛС.\n\n"
                f"Тип сообщения: {message.content_type}\n"
                f"Ошибка: {error}"
            )

        try:
            sent = await bot.send_message(chat_id=user.telegram_id, text=self._limit_text(fallback_text))
            return sent.message_id
        except TelegramAPIError as send_error:
            await self.notify_delivery_error(bot, user, send_error)
            return None

    async def notify_delivery_error(self, bot: Bot, user: User, error: Exception) -> None:
        logger.error("Failed to deliver support message to telegram_id=%s: %s", user.telegram_id, error)
        if not user.topic_id:
            return
        try:
            await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=user.topic_id,
                text=(
                    "Не удалось отправить сообщение пользователю. "
                    "Возможно, пользователь заблокировал бота или недоступен."
                ),
            )
        except TelegramAPIError as notify_error:
            logger.error(
                "Failed to notify support about delivery error topic_id=%s telegram_id=%s: %s",
                user.topic_id,
                user.telegram_id,
                notify_error,
            )

    async def _notify_ticket_media_copy_error(
        self,
        bot: Bot,
        topic_id: int,
        source_message_id: int,
        error: Exception,
    ) -> None:
        try:
            await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=topic_id,
                text=(
                    "Не удалось прикрепить медиа из формы тикета.\n\n"
                    f"user_message_id: {source_message_id}\n"
                    f"Ошибка: {error}"
                ),
            )
        except TelegramAPIError as notify_error:
            logger.error(
                "Failed to notify support about ticket media copy error topic_id=%s source_message_id=%s: %s",
                topic_id,
                source_message_id,
                notify_error,
            )

    async def _send_user_edit_fallback(self, bot: Bot, user: User, text: str, reason: str) -> None:
        if not user.topic_id:
            return
        try:
            await bot.send_message(
                chat_id=self.support_chat_id,
                message_thread_id=user.topic_id,
                text=self._limit_text(f"✏️ Пользователь изменил сообщение\n\n{text}\n\nПричина fallback: {reason}"),
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to send user edit fallback telegram_id=%s topic_id=%s: %s",
                user.telegram_id,
                user.topic_id,
                error,
            )

    async def _send_support_edit_fallback(self, bot: Bot, user: User, text: str, reason: str) -> None:
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=self._limit_text(f"✏️ Сообщение поддержки было изменено:\n\n{text}\n\nПричина fallback: {reason}"),
            )
        except TelegramAPIError as error:
            await self.notify_delivery_error(bot, user, error)

    @staticmethod
    def extract_text_or_caption(message: Message) -> str | None:
        return message.text or message.caption

    @staticmethod
    def is_supported_message(message: Message) -> bool:
        return any(
            getattr(message, field, None)
            for field in (
                "text",
                "photo",
                "video",
                "document",
                "voice",
                "audio",
                "sticker",
                "animation",
            )
        )

    @staticmethod
    def is_topic_unavailable_error(error: Exception) -> bool:
        text = str(error).casefold()
        return any(
            marker in text
            for marker in (
                "message thread not found",
                "thread not found",
                "message thread closed",
                "topic was deleted",
                "topic not found",
            )
        )

    @staticmethod
    def is_user_unavailable_error(error: Exception) -> bool:
        text = str(error).casefold()
        return any(
            marker in text
            for marker in (
                "bot was blocked by the user",
                "user is deactivated",
                "chat not found",
                "forbidden",
            )
        )

    @staticmethod
    def _limit_text(text: str) -> str:
        if len(text) <= 4096:
            return text
        return text[:4000] + "\n\n...текст обрезан, потому что Telegram ограничивает длину сообщения."
