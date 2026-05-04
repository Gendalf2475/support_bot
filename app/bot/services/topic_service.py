from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.database.models import Platform, User
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

    async def sync_topic_title(self, bot: Bot, user: User) -> None:
        if not user.topic_id:
            return

        title = self.build_topic_title(user)
        try:
            await bot.edit_forum_topic(
                chat_id=self.support_chat_id,
                message_thread_id=user.topic_id,
                name=title,
            )
        except TelegramAPIError as error:
            logger.error(
                "Failed to rename topic topic_id=%s platform=%s platform_user_id=%s title=%s: %s",
                user.topic_id,
                user.platform,
                user.platform_user_id,
                title,
                error,
            )
            return

        logger.info("Renamed topic topic_id=%s platform=%s platform_user_id=%s title=%s", user.topic_id, user.platform, user.platform_user_id, title)

    async def recreate_topic(self, bot: Bot, user: User) -> tuple[int, bool]:
        logger.warning(
            "Topic for user platform=%s platform_user_id=%s is unavailable. Creating a new topic.",
            user.platform,
            user.platform_user_id,
        )
        return await self.create_topic(bot, user)

    async def create_topic(self, bot: Bot, user: User) -> tuple[int, bool]:
        title = self.build_topic_title(user)

        try:
            topic = await bot.create_forum_topic(chat_id=self.support_chat_id, name=title)
        except TelegramAPIError as error:
            logger.error(
                "Failed to create topic for platform=%s platform_user_id=%s in support_chat_id=%s: %s",
                user.platform,
                user.platform_user_id,
                self.support_chat_id,
                error,
            )
            raise TopicCreationError(
                "Не удалось создать топик в группе поддержки. Проверьте, что группа является форумом, "
                "а бот добавлен администратором и имеет право создавать топики."
            ) from error

        await self.user_service.set_topic_id(user, topic.message_thread_id)
        logger.info(
            "Created topic topic_id=%s for platform=%s platform_user_id=%s",
            topic.message_thread_id,
            user.platform,
            user.platform_user_id,
        )

        return topic.message_thread_id, True

    @staticmethod
    def build_topic_title(user: User) -> str:
        prefix = TopicService.platform_prefix(user.platform)
        if user.username:
            username = user.username.strip("@")
            if user.platform == Platform.TELEGRAM.value:
                title = f"{prefix} | @{username}"
            else:
                title = f"{prefix} | {username}"
        else:
            fallback_name = (user.full_name or "Имя пользователя").strip()
            title = f"{prefix} | {fallback_name} | ID {user.platform_user_id or user.telegram_id}"

        title = " ".join(title.split())
        if not title:
            title = f"{prefix} | ID {user.platform_user_id or user.telegram_id}"
        return title[:128]

    @staticmethod
    def platform_prefix(platform: str | None) -> str:
        prefixes = {
            Platform.TELEGRAM.value: "TG",
            Platform.DISCORD.value: "DS",
            Platform.VK.value: "VK",
        }
        return prefixes.get(str(platform or Platform.TELEGRAM.value), str(platform or "USER").upper()[:8])
