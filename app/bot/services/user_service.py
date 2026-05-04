from __future__ import annotations

import logging

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.database.models import User


logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_from_telegram(self, telegram_user: TelegramUser) -> tuple[User, bool]:
        user = await self.get_by_telegram_id(telegram_user.id)
        full_name = telegram_user.full_name or telegram_user.first_name or str(telegram_user.id)
        username = telegram_user.username

        if user is None:
            user = User(
                telegram_id=telegram_user.id,
                username=username,
                full_name=full_name,
            )
            self.session.add(user)
            await self.session.flush()
            logger.info("Created new user telegram_id=%s", telegram_user.id)
            return user, True

        changed = False
        if user.username != username:
            user.username = username
            changed = True
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        if changed:
            await self.session.flush()

        return user, False

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        statement = select(User).where(User.telegram_id == telegram_id)
        return await self.session.scalar(statement)

    async def get_by_topic_id(self, topic_id: int) -> User | None:
        statement = select(User).where(User.topic_id == topic_id)
        return await self.session.scalar(statement)

    async def set_topic_id(self, user: User, topic_id: int) -> None:
        user.topic_id = topic_id
        await self.session.flush()

    async def set_blocked(self, user: User, blocked: bool) -> None:
        user.blocked = blocked
        await self.session.flush()
