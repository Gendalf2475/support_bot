from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config import Settings
from app.bot.services.ticket_service import TicketService


logger = logging.getLogger(__name__)


class TicketMaintenanceScheduler:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        if not self.settings.ticket_reminder_enabled and not self.settings.ticket_auto_close_enabled:
            logger.info("Ticket maintenance scheduler is disabled")
            return

        interval_minutes = max(1, self.settings.ticket_check_interval_minutes)
        self.scheduler.add_job(
            self.run_checks,
            "interval",
            minutes=interval_minutes,
            id="ticket_maintenance",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.start()
        logger.info("Ticket maintenance scheduler started interval_minutes=%s", interval_minutes)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Ticket maintenance scheduler stopped")

    async def run_checks(self) -> None:
        try:
            async with self.sessionmaker() as session:
                ticket_service = TicketService(session, self.settings.support_chat_id)

                if self.settings.ticket_reminder_enabled:
                    reminders_sent = await ticket_service.send_due_reminders(
                        bot=self.bot,
                        reminder_after_hours=self.settings.ticket_reminder_after_hours,
                        reminder_interval_minutes=self.settings.ticket_reminder_interval_minutes,
                    )
                    if reminders_sent:
                        logger.info("Ticket reminders sent count=%s", reminders_sent)

                if self.settings.ticket_auto_close_enabled:
                    tickets_closed = await ticket_service.auto_close_inactive_tickets(
                        bot=self.bot,
                        auto_close_after_days=self.settings.ticket_auto_close_after_days,
                    )
                    if tickets_closed:
                        logger.info("Inactive tickets auto-closed count=%s", tickets_closed)

                await session.commit()
        except Exception as error:
            logger.exception("Ticket maintenance check failed: %s", error)
