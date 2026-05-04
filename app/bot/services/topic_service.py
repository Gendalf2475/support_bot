from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.database.models import User
from app.bot.services.user_service import UserService


logger = logging.getLogger(__name__)


class TopicCreationError(RuntimeError):
    pass


class TopicService:
    def __init__(self, user_service: UserService, support_chat_id: int) -> None:
        self.user_service = user_service
        self.support_chat_id = support_chat_id

    async def ensure_topic(self, bot: Bot, user: User) -> tuple[int, bool]:
        if user.topic_id:
            return user.topic_id, False
        return await self.create_topic(bot, user)

    async def recreate_topic(self, bot: Bot, user: User) -> tuple[int, bool]:
        logger.warning(
            "Topic for user telegram_id=%s is unavailable. Creating a new topic.",
            user.telegram_id,
        )
        return await self.create_topic(bot, user)

    async def create_topic(self, bot: Bot, user: User) -> tuple[int, bool]:
        title = self.build_topic_title(user)

        try:
            topic = await bot.create_forum_topic(chat_id=self.support_chat_id, name=title)
        except TelegramAPIError as error:
            logger.error(
                "Failed to create topic for telegram_id=%s in support_chat_id=%s: %s",
                user.telegram_id,
                self.support_chat_id,
                error,
            )
            raise TopicCreationError(
                "Не удалось создать топик в группе поддержки. Проверьте, что группа является форумом, "
                "а бот добавлен администратором и имеет право создавать топики."
            ) from error

        await self.user_service.set_topic_id(user, topic.message_thread_id)
        logger.info(
            "Created topic topic_id=%s for telegram_id=%s",
            topic.message_thread_id,
            user.telegram_id,
        )

        return topic.message_thread_id, True

    @staticmethod
    def build_topic_title(user: User) -> str:
        if user.username:
            title = f"@{user.username.strip('@')}"
        else:
            fallback_name = (user.full_name or "Имя пользователя").strip()
            title = f"{fallback_name} | ID {user.telegram_id}"

        title = " ".join(title.split())
        if not title:
            title = f"User ID {user.telegram_id}"
        return title[:128]
