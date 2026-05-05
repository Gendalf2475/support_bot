from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram.types import User as TelegramUser
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.database.models import Platform, User, utcnow


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
    old_platform_user_id: str | None = None
    new_platform_user_id: str | None = None


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create_from_telegram(self, telegram_user: TelegramUser) -> tuple[User, bool]:
        result = await self.upsert_user_from_telegram(telegram_user)
        return result.user, result.created

    async def upsert_user_from_telegram(self, telegram_user: TelegramUser) -> UserUpsertResult:
        result = await self.upsert_user(
            platform=Platform.TELEGRAM.value,
            platform_user_id=str(telegram_user.id),
            username=telegram_user.username,
            full_name=self.normalize_full_name(telegram_user),
            telegram_id=telegram_user.id,
        )
        return result

    async def upsert_user(
        self,
        platform: str,
        platform_user_id: str,
        username: str | None = None,
        full_name: str | None = None,
        telegram_id: int | None = None,
    ) -> UserUpsertResult:
        normalized_platform = self.normalize_platform(platform)
        normalized_platform_user_id = str(platform_user_id).strip()
        if not normalized_platform_user_id:
            raise ValueError("platform_user_id must not be empty")

        user = await self.get_by_platform_user_id(normalized_platform, normalized_platform_user_id)
        if user is None and normalized_platform == Platform.TELEGRAM.value and telegram_id is not None:
            user = await self.get_by_telegram_id(telegram_id)

        normalized_username = self.normalize_username(username)
        normalized_full_name = self.normalize_generic_full_name(full_name, normalized_platform_user_id)
        now = utcnow()
        if user is None:
            user = User(
                telegram_id=telegram_id if normalized_platform == Platform.TELEGRAM.value else None,
                platform=normalized_platform,
                platform_user_id=normalized_platform_user_id,
                username=normalized_username,
                full_name=normalized_full_name,
                updated_at=now,
            )
            self.session.add(user)
            await self.session.flush()
            logger.info("Created new user platform=%s platform_user_id=%s", normalized_platform, normalized_platform_user_id)
            return UserUpsertResult(
                user=user,
                created=True,
                changed=True,
                old_username=None,
                new_username=normalized_username,
                old_full_name=None,
                new_full_name=normalized_full_name,
                old_platform_user_id=None,
                new_platform_user_id=normalized_platform_user_id,
            )

        old_username = user.username
        old_full_name = user.full_name
        old_platform_user_id = user.platform_user_id
        changed = (
            old_username != normalized_username
            or old_full_name != normalized_full_name
            or user.platform != normalized_platform
            or old_platform_user_id != normalized_platform_user_id
        )
        user.platform = normalized_platform
        user.platform_user_id = normalized_platform_user_id
        if normalized_platform == Platform.TELEGRAM.value:
            user.telegram_id = telegram_id
        user.username = normalized_username
        user.full_name = normalized_full_name
        user.updated_at = now
        await self.session.flush()

        if changed:
            logger.info(
                "Updated user profile platform=%s platform_user_id=%s username=%s->%s full_name=%s->%s",
                normalized_platform,
                normalized_platform_user_id,
                old_username,
                normalized_username,
                old_full_name,
                normalized_full_name,
            )

        return UserUpsertResult(
            user=user,
            created=False,
            changed=changed,
            old_username=old_username,
            new_username=normalized_username,
            old_full_name=old_full_name,
            new_full_name=normalized_full_name,
            old_platform_user_id=old_platform_user_id,
            new_platform_user_id=normalized_platform_user_id,
        )

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        statement = select(User).where(
            or_(
                User.telegram_id == telegram_id,
                (User.platform == Platform.TELEGRAM.value) & (User.platform_user_id == str(telegram_id)),
            )
        )
        return await self.session.scalar(statement)

    async def get_by_platform_user_id(self, platform: str, platform_user_id: str) -> User | None:
        statement = select(User).where(
            User.platform == self.normalize_platform(platform),
            User.platform_user_id == str(platform_user_id),
        )
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

    async def set_minecraft_nickname(self, user: User, nickname: str) -> None:
        normalized = str(nickname or "").strip()
        if not normalized:
            return
        now = utcnow()
        user.minecraft_nickname = normalized
        user.minecraft_nickname_updated_at = now
        user.updated_at = now
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

    @staticmethod
    def normalize_generic_full_name(full_name: str | None, fallback_id: str) -> str:
        normalized = str(full_name or "").strip()
        return normalized or str(fallback_id)

    @staticmethod
    def normalize_platform(platform: str) -> str:
        normalized = str(platform or "").strip().lower()
        if normalized not in {Platform.TELEGRAM.value, Platform.DISCORD.value, Platform.VK.value}:
            raise ValueError(f"Unsupported platform: {platform}")
        return normalized
