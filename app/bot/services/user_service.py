from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.database.models import User, utcnow


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserUpsertResult:
    user: User
    created: bool
    changed: bool
    old_username: str | None
    new_username: str | None
    old_full_name: str | None
    new_full_name: str


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_from_telegram(self, telegram_user: TelegramUser) -> tuple[User, bool]:
        result = await self.upsert_user_from_telegram(telegram_user)
        return result.user, result.created

    async def upsert_user_from_telegram(self, telegram_user: TelegramUser) -> UserUpsertResult:
        user = await self.get_by_telegram_id(telegram_user.id)
        full_name = self.normalize_full_name(telegram_user)
        username = self.normalize_username(telegram_user.username)
        now = utcnow()

        if user is None:
            user = User(
                telegram_id=telegram_user.id,
                username=username,
                full_name=full_name,
                updated_at=now,
            )
            self.session.add(user)
            await self.session.flush()
            logger.info("Created new user telegram_id=%s", telegram_user.id)
            return UserUpsertResult(
                user=user,
                created=True,
                changed=True,
                old_username=None,
                new_username=username,
                old_full_name=None,
                new_full_name=full_name,
            )

        old_username = user.username
        old_full_name = user.full_name
        changed = old_username != username or old_full_name != full_name
        user.username = username
        user.full_name = full_name
        user.updated_at = now
        await self.session.flush()

        if changed:
            logger.info(
                "Updated user profile telegram_id=%s username=%s->%s full_name=%s->%s",
                telegram_user.id,
                old_username,
                username,
                old_full_name,
                full_name,
            )

        return UserUpsertResult(
            user=user,
            created=False,
            changed=changed,
            old_username=old_username,
            new_username=username,
            old_full_name=old_full_name,
            new_full_name=full_name,
        )

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

    @staticmethod
    def normalize_username(username: str | None) -> str | None:
        if not username:
            return None
        normalized = username.strip().lstrip("@")
        return normalized or None

    @staticmethod
    def normalize_full_name(telegram_user: TelegramUser) -> str:
        full_name = str(telegram_user.full_name or telegram_user.first_name or "").strip()
        return full_name or str(telegram_user.id)
