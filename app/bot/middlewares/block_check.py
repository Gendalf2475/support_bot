from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config import Settings
from app.bot.services.topic_service import TopicService
from app.bot.services.user_service import UserService


class DatabaseSessionMiddleware(BaseMiddleware):
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with self.sessionmaker() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise


class BlockCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        session = data.get("session")
        if session:
            telegram_user = self.get_private_telegram_user(event)
            if telegram_user is not None:
                user_service = UserService(session)
                result = await user_service.upsert_user_from_telegram(telegram_user)
                data["known_user"] = result.user
                data["is_blocked_user"] = bool(result.user.blocked)

                bot = data.get("bot")
                settings = data.get("settings")
                if result.changed and result.user.topic_id and isinstance(bot, Bot) and isinstance(settings, Settings):
                    await TopicService(user_service, settings.support_chat_id).sync_topic_title(bot, result.user)

        return await handler(event, data)

    @staticmethod
    def get_private_telegram_user(event: TelegramObject) -> Any | None:
        if isinstance(event, Message) and event.chat.type == ChatType.PRIVATE and event.from_user:
            return event.from_user
        if (
            isinstance(event, CallbackQuery)
            and isinstance(event.message, Message)
            and event.message.chat.type == ChatType.PRIVATE
        ):
            return event.from_user
        return None
