from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.config import Settings
from app.bot.services.ticket_form_service import TicketFormService
from app.bot.services.platform_router import PlatformRouter
from app.bot.services.ticket_service import TicketService


logger = logging.getLogger(__name__)


class TicketMaintenanceScheduler:
    def __init__(
        self,
        bot: Bot,
        sessionmaker: async_sessionmaker[AsyncSession],
        settings: Settings,
        ticket_form_service: TicketFormService,
        platform_router: PlatformRouter | None = None,
    ) -> None:
        self.bot = bot
        self.sessionmaker = sessionmaker
        self.settings = settings
        self.ticket_form_service = ticket_form_service
        self.platform_router = platform_router
        self.scheduler = AsyncIOScheduler(timezone="UTC")

    def start(self) -> None:
        if not self.has_enabled_jobs():
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

                if self.settings.ticket_auto_close_enabled and self.settings.ticket_auto_close_warning_enabled:
                    warnings_sent = await ticket_service.send_auto_close_warnings(
                        bot=self.bot,
                        auto_close_after_days=self.settings.ticket_auto_close_after_days,
                        warning_hours=self.settings.ticket_auto_close_warning_hours,
                        platform_router=self.platform_router,
                    )
                    if warnings_sent:
                        logger.info("Auto-close warnings sent count=%s", warnings_sent)

                if self.settings.ticket_auto_close_enabled:
                    tickets_closed = await ticket_service.auto_close_inactive_tickets(
                        bot=self.bot,
                        auto_close_after_days=self.settings.ticket_auto_close_after_days,
                        ticket_forms=self.ticket_form_service.get_forms() if self.ticket_form_service.enabled else None,
                        platform_router=self.platform_router,
                    )
                    if tickets_closed:
                        logger.info("Inactive tickets auto-closed count=%s", tickets_closed)

                if self.settings.user_reply_reminder_enabled:
                    user_reminders_sent = await ticket_service.send_waiting_user_reminders(
                        bot=self.bot,
                        reminder_after_hours=self.settings.user_reply_reminder_after_hours,
                        reminder_interval_hours=self.settings.user_reply_reminder_interval_hours,
                        platform_router=self.platform_router,
                    )
                    if user_reminders_sent:
                        logger.info("Waiting-user reminders sent count=%s", user_reminders_sent)

                if self.settings.ticket_reminder_enabled:
                    reminders_sent = await ticket_service.send_due_reminders(
                        bot=self.bot,
                        reminder_after_hours=self.settings.ticket_reminder_after_hours,
                        reminder_interval_minutes=self.settings.ticket_reminder_interval_minutes,
                    )
                    if reminders_sent:
                        logger.info("Ticket reminders sent count=%s", reminders_sent)

                await session.commit()
        except Exception as error:
            logger.exception("Ticket maintenance check failed: %s", error)

    def has_enabled_jobs(self) -> bool:
        return any(
            (
                self.settings.ticket_reminder_enabled,
                self.settings.ticket_auto_close_enabled,
                self.settings.user_reply_reminder_enabled,
            )
        )
