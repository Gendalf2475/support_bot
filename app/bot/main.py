from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import text

from app.bot.channels.errors import ERROR_TEMPORARY_NETWORK, ERROR_UNEXPECTED, classify_channel_error
from app.bot.channels.discord_channel import DiscordChannel
from app.bot.channels.telegram_channel import TelegramChannel
from app.bot.channels.vk_channel import VKChannel
from app.bot.config import Settings, get_settings
from app.bot.database.session import create_sessionmaker
from app.bot.handlers import admin, support_messages, user_messages
from app.bot.middlewares.block_check import BlockCheckMiddleware, DatabaseSessionMiddleware
from app.bot.services.external_support import ExternalSupportProcessor
from app.bot.services.minecraft_service import MinecraftService
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.channel_supervisor import ChannelFailureNotifier, ChannelHealthRegistry, ChannelSupervisor
from app.bot.services.ticket_form_service import TicketFormService
from app.bot.services.ticket_scheduler import TicketMaintenanceScheduler


logger = logging.getLogger(__name__)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)

    logger.info("Starting support bot")
    bot: Bot | None = None
    discord_channel: DiscordChannel | None = None
    vk_channel: VKChannel | None = None
    ticket_scheduler: TicketMaintenanceScheduler | None = None
    channel_supervisor: ChannelSupervisor | None = None
    channel_tasks: list[asyncio.Task[None]] = []
    exit_code = 0

    try:
        bot = Bot(token=settings.bot_token)
        dispatcher = Dispatcher()

        sessionmaker = create_sessionmaker(settings.database_url)
        await verify_database_connection(sessionmaker)
        ticket_form_service = TicketFormService(
            settings.ticket_forms_path,
            global_questions_enabled=settings.minecraft_nickname_required_enabled,
        )
        minecraft_service = MinecraftService(settings)
        platform_router = PlatformRouter()
        external_processor = ExternalSupportProcessor(bot, sessionmaker, settings, ticket_form_service, platform_router, minecraft_service)
        telegram_channel = TelegramChannel(bot)
        discord_channel = DiscordChannel(settings, external_processor)
        vk_channel = VKChannel(settings, external_processor)
        platform_router.register(telegram_channel)
        platform_router.register(discord_channel)
        platform_router.register(vk_channel)
        ticket_scheduler = TicketMaintenanceScheduler(bot, sessionmaker, settings, ticket_form_service, platform_router)

        notifier = ChannelFailureNotifier(bot, settings)
        health_registry = ChannelHealthRegistry()
        health_registry.ensure("telegram", "Telegram", enabled=settings.telegram_enabled)
        health_registry.ensure("discord", "Discord", enabled=settings.discord_enabled)
        health_registry.ensure("vk", "VK", enabled=settings.vk_enabled and settings.vk_longpoll_enabled)
        vk_channel.set_supervision(failure_notifier=notifier, health_registry=health_registry)

        dispatcher["settings"] = settings
        dispatcher["dispatcher"] = dispatcher
        dispatcher["ticket_form_service"] = ticket_form_service
        dispatcher["platform_router"] = platform_router
        dispatcher["minecraft_service"] = minecraft_service
        dispatcher["ticket_scheduler"] = ticket_scheduler

        db_middleware = DatabaseSessionMiddleware(sessionmaker)
        block_check_middleware = BlockCheckMiddleware()
        dispatcher.message.outer_middleware(db_middleware)
        dispatcher.message.outer_middleware(block_check_middleware)
        dispatcher.edited_message.outer_middleware(db_middleware)
        dispatcher.edited_message.outer_middleware(block_check_middleware)
        dispatcher.callback_query.outer_middleware(db_middleware)
        dispatcher.callback_query.outer_middleware(block_check_middleware)

        dispatcher.include_router(admin.router)
        dispatcher.include_router(user_messages.router)
        dispatcher.include_router(support_messages.router)

        ticket_scheduler.start()
        await bot.delete_webhook(drop_pending_updates=True)

        if settings.channel_supervisor_enabled:
            channel_supervisor = ChannelSupervisor(settings, notifier, health=health_registry)
            dispatcher["channel_supervisor"] = channel_supervisor
            channel_supervisor.add_channel(
                "telegram",
                "Telegram",
                lambda: dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types()),
                stopper=lambda: stop_telegram_polling(dispatcher),
                enabled=settings.telegram_enabled,
            )
            channel_supervisor.add_channel(
                "discord",
                "Discord",
                discord_channel.start,
                stopper=discord_channel.stop,
                enabled=settings.discord_enabled,
            )
            channel_supervisor.add_channel(
                "vk",
                "VK",
                vk_channel.start,
                stopper=vk_channel.stop,
                enabled=settings.vk_enabled and settings.vk_longpoll_enabled,
            )
            channel_supervisor.start()
            exit_code = await channel_supervisor.wait()
        else:
            channel_tasks = start_optional_channels(discord_channel, vk_channel, notifier)
            await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    except asyncio.CancelledError:
        raise
    except Exception as error:
        exit_code = 1
        logger.critical("Critical bot failure, exiting process: %s", error, exc_info=True)
        if bot is not None:
            await notify_critical_failure(bot, settings, error)
    finally:
        if ticket_scheduler is not None:
            ticket_scheduler.shutdown()
        if channel_supervisor is not None:
            await channel_supervisor.stop()
        for task in channel_tasks:
            task.cancel()
        if channel_tasks:
            await asyncio.gather(*channel_tasks, return_exceptions=True)
        if discord_channel is not None:
            await discord_channel.stop()
        if vk_channel is not None:
            await vk_channel.stop()
        if bot is not None:
            await bot.session.close()

    if exit_code:
        raise SystemExit(exit_code)


def start_optional_channels(
    discord_channel: DiscordChannel,
    vk_channel: VKChannel,
    notifier: ChannelFailureNotifier,
) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for name, starter in (
        ("discord", discord_channel.start),
        ("vk", vk_channel.start),
    ):
        task = asyncio.create_task(run_channel(name, starter, notifier))
        tasks.append(task)
    return tasks


async def run_channel(name: str, starter: Callable[[], Awaitable[None]], notifier: ChannelFailureNotifier) -> None:
    try:
        await starter()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        info = classify_channel_error(name, error)
        if info.error_type == ERROR_TEMPORARY_NETWORK:
            logger.warning(
                "Channel stopped by temporary network error channel=%s error_type=%s error=%s",
                name,
                info.error_type,
                error,
            )
        elif info.error_type == ERROR_UNEXPECTED:
            logger.exception("Channel crashed channel=%s error_type=%s", name, info.error_type)
        else:
            logger.error("Channel crashed channel=%s error_type=%s error=%s", name, info.error_type, error)
        await notifier.notify_failure(name, info.error_type)


async def stop_telegram_polling(dispatcher: Dispatcher) -> None:
    stop_polling = getattr(dispatcher, "stop_polling", None)
    if not callable(stop_polling):
        return
    try:
        await stop_polling()
    except RuntimeError as error:
        logger.debug("Telegram polling is not running: %s", error)


async def notify_critical_failure(bot: Bot, settings: Settings, error: Exception) -> None:
    if not settings.channel_failure_notify_enabled:
        return
    try:
        await bot.send_message(
            chat_id=settings.support_chat_id,
            text=f"⚠️ Бот завершает процесс из-за критической ошибки. Docker должен перезапустить контейнер.\n\n{type(error).__name__}: {error}",
        )
    except TelegramAPIError as notify_error:
        logger.error("Failed to notify support chat about critical failure: %s", notify_error)


async def verify_database_connection(sessionmaker: Any) -> None:
    async with sessionmaker() as session:
        await session.execute(text("SELECT 1"))


if __name__ == "__main__":
    asyncio.run(main())
