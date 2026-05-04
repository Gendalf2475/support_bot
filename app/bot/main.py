from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher

from app.bot.channels.discord_channel import DiscordChannel
from app.bot.channels.telegram_channel import TelegramChannel
from app.bot.channels.vk_channel import VKChannel
from app.bot.config import get_settings
from app.bot.database.session import create_sessionmaker
from app.bot.handlers import admin, support_messages, user_messages
from app.bot.middlewares.block_check import BlockCheckMiddleware, DatabaseSessionMiddleware
from app.bot.services.external_support import ExternalSupportProcessor
from app.bot.services.platform_router import PlatformRouter
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
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()

    sessionmaker = create_sessionmaker(settings.database_url)
    ticket_form_service = TicketFormService(settings.ticket_forms_path)
    platform_router = PlatformRouter()
    external_processor = ExternalSupportProcessor(bot, sessionmaker, settings, ticket_form_service, platform_router)
    telegram_channel = TelegramChannel(bot)
    discord_channel = DiscordChannel(settings, external_processor)
    vk_channel = VKChannel(settings, external_processor)
    platform_router.register(telegram_channel)
    platform_router.register(discord_channel)
    platform_router.register(vk_channel)
    ticket_scheduler = TicketMaintenanceScheduler(bot, sessionmaker, settings, ticket_form_service, platform_router)

    dispatcher["settings"] = settings
    dispatcher["ticket_form_service"] = ticket_form_service
    dispatcher["platform_router"] = platform_router

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

    channel_tasks: list[asyncio.Task[None]] = []
    try:
        channel_tasks = start_optional_channels(discord_channel, vk_channel)
        ticket_scheduler.start()
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        ticket_scheduler.shutdown()
        for task in channel_tasks:
            task.cancel()
        await discord_channel.stop()
        await vk_channel.stop()
        await bot.session.close()


def start_optional_channels(discord_channel: DiscordChannel, vk_channel: VKChannel) -> list[asyncio.Task[None]]:
    tasks: list[asyncio.Task[None]] = []
    for name, starter in (
        ("discord", discord_channel.start),
        ("vk", vk_channel.start),
    ):
        task = asyncio.create_task(run_channel(name, starter))
        tasks.append(task)
    return tasks


async def run_channel(name: str, starter: Callable[[], Awaitable[None]]) -> None:
    try:
        await starter()
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.exception("%s channel crashed: %s", name, error)


if __name__ == "__main__":
    asyncio.run(main())
