from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from app.bot.channels.base import OutgoingMessage, SentMessageRef
from app.bot.database.models import Platform


logger = logging.getLogger(__name__)


class TelegramChannel:
    platform = Platform.TELEGRAM.value

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send_message(self, message: OutgoingMessage) -> SentMessageRef | None:
        try:
            sent = await self.bot.send_message(chat_id=int(message.platform_user_id), text=message.text or "")
            return SentMessageRef(platform_message_id=str(sent.message_id))
        except TelegramForbiddenError as error:
            logger.warning("Failed to deliver Telegram channel message user_id=%s: %s", message.platform_user_id, error)
            return None
        except (TelegramAPIError, ValueError) as error:
            logger.error("Failed to send Telegram channel message user_id=%s: %s", message.platform_user_id, error)
            return None
