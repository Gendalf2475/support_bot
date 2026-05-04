from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from app.bot.config import get_settings
from app.bot.database.session import create_sessionmaker
from app.bot.handlers import admin, support_messages, user_messages
from app.bot.middlewares.block_check import BlockCheckMiddleware, DatabaseSessionMiddleware
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
    ticket_scheduler = TicketMaintenanceScheduler(bot, sessionmaker, settings, ticket_form_service)

    dispatcher["settings"] = settings
    dispatcher["ticket_form_service"] = ticket_form_service

    db_middleware = DatabaseSessionMiddleware(sessionmaker)
    block_check_middleware = BlockCheckMiddleware()
    dispatcher.message.outer_middleware(db_middleware)
    dispatcher.message.outer_middleware(block_check_middleware)
    dispatcher.edited_message.outer_middleware(db_middleware)
    dispatcher.edited_message.outer_middleware(block_check_middleware)
    dispatcher.callback_query.outer_middleware(db_middleware)

    dispatcher.include_router(admin.router)
    dispatcher.include_router(user_messages.router)
    dispatcher.include_router(support_messages.router)

    try:
        ticket_scheduler.start()
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        ticket_scheduler.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
