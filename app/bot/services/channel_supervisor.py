from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.bot.channels.errors import (
    ERROR_AUTH,
    ERROR_INTENTS,
    ERROR_PERMISSION,
    ERROR_POLLING_CONFLICT,
    ERROR_TEMPORARY_NETWORK,
    classify_channel_error,
)
from app.bot.config import Settings
from app.bot.database.models import utcnow


logger = logging.getLogger(__name__)

CHANNEL_STATUS_WORKING = "working"
CHANNEL_STATUS_RECONNECTING = "reconnecting"
CHANNEL_STATUS_DISABLED = "disabled"
CHANNEL_STATUS_ERROR = "error"
CHANNEL_STATUS_STARTING = "starting"

VK_AUTH_RETRY_DELAY_SECONDS = 300


@dataclass
class ChannelHealth:
    name: str
    display_name: str
    status: str = CHANNEL_STATUS_DISABLED
    last_error_type: str | None = None
    last_error_message: str | None = None
    last_error_at: datetime | None = None
    consecutive_errors: int = 0
    last_restored_at: datetime | None = None


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    display_name: str
    starter: Callable[[], Awaitable[None]]
    stopper: Callable[[], Awaitable[None]] | None = None
    enabled: bool = True


class ChannelHealthRegistry:
    def __init__(self) -> None:
        self._channels: dict[str, ChannelHealth] = {}

    def ensure(self, name: str, display_name: str, *, enabled: bool = True) -> ChannelHealth:
        health = self._channels.get(name)
        if health is None:
            health = ChannelHealth(
                name=name,
                display_name=display_name,
                status=CHANNEL_STATUS_STARTING if enabled else CHANNEL_STATUS_DISABLED,
            )
            self._channels[name] = health
        else:
            health.display_name = display_name
            if not enabled:
                health.status = CHANNEL_STATUS_DISABLED
        return health

    def mark_disabled(self, name: str, display_name: str) -> None:
        health = self.ensure(name, display_name, enabled=False)
        health.status = CHANNEL_STATUS_DISABLED

    def mark_starting(self, name: str) -> None:
        self._channels[name].status = CHANNEL_STATUS_STARTING

    def mark_working(self, name: str) -> None:
        self._channels[name].status = CHANNEL_STATUS_WORKING

    def mark_reconnecting(
        self,
        name: str,
        *,
        error_type: str | None = None,
        error: BaseException | str | None = None,
        consecutive_errors: int | None = None,
    ) -> None:
        health = self._channels[name]
        health.status = CHANNEL_STATUS_RECONNECTING
        if error_type is not None:
            health.last_error_type = error_type
        if error is not None:
            health.last_error_message = _short_error(error)
            health.last_error_at = utcnow()
        if consecutive_errors is not None:
            health.consecutive_errors = consecutive_errors

    def mark_error(
        self,
        name: str,
        *,
        error_type: str,
        error: BaseException | str,
        consecutive_errors: int,
    ) -> None:
        health = self._channels[name]
        health.status = CHANNEL_STATUS_ERROR
        health.last_error_type = error_type
        health.last_error_message = _short_error(error)
        health.last_error_at = utcnow()
        health.consecutive_errors = consecutive_errors

    def mark_restored(self, name: str) -> None:
        health = self._channels[name]
        health.status = CHANNEL_STATUS_WORKING
        health.consecutive_errors = 0
        health.last_restored_at = utcnow()

    def snapshot(self) -> list[ChannelHealth]:
        return list(self._channels.values())

    def get(self, name: str) -> ChannelHealth | None:
        return self._channels.get(name)


class ChannelFailureNotifier:
    def __init__(self, bot: Bot, settings: Settings) -> None:
        self.bot = bot
        self.settings = settings
        self._notified_at: dict[tuple[str, str], float] = {}
        self._failure_notified: dict[str, bool] = {}

    async def notify_failure(self, channel_name: str, error_type: str, *, text: str | None = None) -> bool:
        if not self.settings.channel_failure_notify_enabled:
            return False

        cooldown_seconds = max(0, self.settings.channel_failure_notify_cooldown_minutes) * 60
        key = (channel_name, error_type)
        now = time.monotonic()
        last_notified_at = self._notified_at.get(key)
        if last_notified_at is not None and now - last_notified_at < cooldown_seconds:
            return False

        self._notified_at[key] = now
        self._failure_notified[channel_name] = True
        await self._send(text or failure_notification_text(channel_name, error_type))
        return True

    async def notify_restored(self, channel_name: str) -> None:
        if not self.settings.channel_failure_notify_enabled:
            return
        if not self._failure_notified.get(channel_name):
            return

        await self._send(restored_notification_text(channel_name))
        self._failure_notified[channel_name] = False

    async def _send(self, text: str) -> None:
        try:
            await self.bot.send_message(chat_id=self.settings.support_chat_id, text=text)
        except TelegramAPIError as error:
            logger.error("Failed to notify support chat about channel state: %s", error)


class ChannelSupervisor:
    def __init__(
        self,
        settings: Settings,
        notifier: ChannelFailureNotifier,
        *,
        health: ChannelHealthRegistry | None = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.health = health or ChannelHealthRegistry()
        self._specs: list[ChannelSpec] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._shutdown_event = asyncio.Event()
        self._stopping = False
        self._exit_code = 0

    def add_channel(
        self,
        name: str,
        display_name: str,
        starter: Callable[[], Awaitable[None]],
        *,
        stopper: Callable[[], Awaitable[None]] | None = None,
        enabled: bool = True,
    ) -> None:
        self.health.ensure(name, display_name, enabled=enabled)
        self._specs.append(ChannelSpec(name=name, display_name=display_name, starter=starter, stopper=stopper, enabled=enabled))

    def start(self) -> None:
        for spec in self._specs:
            if not spec.enabled:
                self.health.mark_disabled(spec.name, spec.display_name)
                continue
            self._tasks.append(asyncio.create_task(self._run_channel(spec), name=f"{spec.name}-supervisor"))

    async def wait(self) -> int:
        await self._shutdown_event.wait()
        return self._exit_code

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for spec in self._specs:
            if spec.stopper is not None:
                try:
                    await spec.stopper()
                except Exception as error:
                    logger.exception("Failed to stop channel channel=%s: %s", spec.name, error)

    def snapshot(self) -> list[ChannelHealth]:
        return self.health.snapshot()

    async def _run_channel(self, spec: ChannelSpec) -> None:
        restart_attempt = 0
        stable_seconds = max(1, self.settings.channel_restart_delay_seconds)

        while not self._stopping:
            self.health.mark_starting(spec.name)
            started_at = time.monotonic()
            restore_task = asyncio.create_task(self._mark_restored_after_stable(spec.name, stable_seconds))
            try:
                await spec.starter()
            except asyncio.CancelledError:
                restore_task.cancel()
                await asyncio.gather(restore_task, return_exceptions=True)
                raise
            except Exception as error:
                restore_task.cancel()
                await asyncio.gather(restore_task, return_exceptions=True)
                if time.monotonic() - started_at >= stable_seconds:
                    restart_attempt = 0
                restart_attempt += 1
                info = classify_channel_error(spec.name, error)
                self.health.mark_error(
                    spec.name,
                    error_type=info.error_type,
                    error=error,
                    consecutive_errors=restart_attempt,
                )
                logger.exception("Channel crashed channel=%s error_type=%s", spec.name, info.error_type)
                await self.notifier.notify_failure(spec.name, info.error_type)
            else:
                restore_task.cancel()
                await asyncio.gather(restore_task, return_exceptions=True)
                if self._stopping:
                    return
                if time.monotonic() - started_at >= stable_seconds:
                    restart_attempt = 0
                restart_attempt += 1
                error = RuntimeError("channel exited unexpectedly")
                self.health.mark_error(
                    spec.name,
                    error_type=ERROR_TEMPORARY_NETWORK,
                    error=error,
                    consecutive_errors=restart_attempt,
                )
                logger.error("Channel crashed channel=%s error_type=%s", spec.name, ERROR_TEMPORARY_NETWORK)
                await self.notifier.notify_failure(spec.name, ERROR_TEMPORARY_NETWORK)

            if not self.settings.channel_restart_enabled:
                await self._exit_process(spec.name)
                return

            max_attempts = max(0, self.settings.channel_restart_max_attempts)
            if max_attempts > 0 and restart_attempt >= max_attempts:
                logger.critical("Channel restart attempts exceeded channel=%s exiting process", spec.name)
                exceeded_error_type = f"{self.health.get(spec.name).last_error_type if self.health.get(spec.name) else ERROR_TEMPORARY_NETWORK}_restart_exceeded"
                await self.notifier.notify_failure(
                    spec.name,
                    exceeded_error_type,
                    text=f"⚠️ {spec.display_name}-канал не удалось восстановить. Бот перезапускает контейнер.",
                )
                await self._exit_process(spec.name)
                return

            if spec.stopper is not None:
                try:
                    await spec.stopper()
                except Exception as error:
                    logger.exception("Failed to cleanup crashed channel channel=%s: %s", spec.name, error)

            delay = self._restart_delay(spec.name, restart_attempt)
            self.health.mark_reconnecting(spec.name, consecutive_errors=restart_attempt)
            logger.info("Restarting channel channel=%s attempt=%s delay=%s", spec.name, restart_attempt, delay)
            await asyncio.sleep(delay)

    async def _mark_restored_after_stable(self, channel_name: str, stable_seconds: int) -> None:
        await asyncio.sleep(stable_seconds)
        health = self.health.get(channel_name)
        if health is None or health.status not in {CHANNEL_STATUS_STARTING, CHANNEL_STATUS_WORKING}:
            return
        should_notify = bool(health and health.consecutive_errors > 0)
        self.health.mark_restored(channel_name)
        if should_notify:
            logger.info("Channel restored channel=%s", channel_name)
            await self.notifier.notify_restored(channel_name)

    def _restart_delay(self, channel_name: str, attempt: int) -> int:
        health = self.health.get(channel_name)
        if channel_name == "vk" and health is not None and health.last_error_type in {ERROR_AUTH, ERROR_PERMISSION}:
            return VK_AUTH_RETRY_DELAY_SECONDS

        base_delay = max(0, self.settings.channel_restart_delay_seconds)
        max_delay = max(base_delay, self.settings.channel_restart_max_delay_seconds)
        if attempt <= 1:
            return min(base_delay, max_delay)
        return min(base_delay * (2 ** (attempt - 1)), max_delay)

    async def _exit_process(self, channel_name: str) -> None:
        logger.critical("Critical channel failure channel=%s exiting process", channel_name)
        self._exit_code = 1
        self._shutdown_event.set()


def failure_notification_text(channel_name: str, error_type: str) -> str:
    if channel_name == "vk":
        if error_type == ERROR_AUTH:
            return "⚠️ VK-канал отключился: ошибка авторизации VK."
        if error_type == ERROR_PERMISSION:
            return "⚠️ VK-канал отключился: нет нужных прав у VK-токена."
        if error_type == ERROR_TEMPORARY_NETWORK:
            return "⚠️ VK-канал отключился: ошибка сети. Бот пытается восстановить соединение."
        return "⚠️ VK-канал отключился: unexpected error. Подробности в логах."

    if channel_name == "discord":
        if error_type == ERROR_TEMPORARY_NETWORK:
            return "⚠️ Discord-канал отключился: ошибка Discord Gateway. Бот пытается восстановить соединение."
        if error_type == ERROR_INTENTS:
            return "⚠️ Discord-канал отключился: не включены нужные Discord intents."
        if error_type == ERROR_AUTH:
            return "⚠️ Discord-канал отключился: ошибка авторизации Discord."
        return "⚠️ Discord-канал отключился: unexpected error. Подробности в логах."

    if channel_name == "telegram":
        if error_type == ERROR_POLLING_CONFLICT:
            return "⚠️ Telegram polling упал: конфликт polling. Бот пытается перезапустить polling."
        if error_type == ERROR_AUTH:
            return "⚠️ Telegram polling упал: ошибка авторизации Telegram."
        return "⚠️ Telegram polling упал. Бот пытается перезапустить polling."

    return f"⚠️ {channel_name}-канал отключился. Бот пытается восстановить соединение."


def restored_notification_text(channel_name: str) -> str:
    if channel_name == "vk":
        return "✅ VK-канал восстановлен."
    if channel_name == "discord":
        return "✅ Discord-канал восстановлен."
    if channel_name == "telegram":
        return "✅ Telegram polling восстановлен."
    return f"✅ {channel_name}-канал восстановлен."


def _short_error(error: BaseException | str) -> str:
    text = str(error)
    if len(text) > 500:
        return f"{text[:497]}..."
    return text
