from __future__ import annotations

import logging
from typing import Any

from app.bot.channels.base import ATTACHMENT_DOCUMENT, ATTACHMENT_PHOTO, ATTACHMENT_VIDEO, Attachment, IncomingMessage, OutgoingMessage, SentMessageRef
from app.bot.config import Settings
from app.bot.database.models import Platform
from app.bot.services.external_support import ExternalSupportProcessor


logger = logging.getLogger(__name__)


class DiscordChannel:
    platform = Platform.DISCORD.value

    def __init__(self, settings: Settings, processor: ExternalSupportProcessor) -> None:
        self.settings = settings
        self.processor = processor
        self.client: Any | None = None

    async def start(self) -> None:
        if not self.settings.discord_enabled:
            return
        try:
            import discord
        except ImportError:
            logger.error("discord.py is not installed; Discord channel is disabled")
            return
        if not self.settings.discord_bot_token:
            logger.error("Discord channel is enabled, but DISCORD_BOT_TOKEN is empty")
            return

        intents = discord.Intents.default()
        intents.dm_messages = True
        intents.message_content = True
        client = discord.Client(intents=intents)
        self.client = client

        @client.event
        async def on_ready() -> None:
            logger.info("Discord bot connected as %s", client.user)

        @client.event
        async def on_message(message: Any) -> None:
            if message.author.bot or message.guild is not None:
                return
            incoming = self.build_incoming(message)
            await self.processor.handle_incoming(incoming)

        try:
            await client.start(self.settings.discord_bot_token)
        except Exception as error:
            logger.exception("Discord channel stopped with error: %s", error)

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def send_message(self, message: OutgoingMessage) -> SentMessageRef | None:
        if self.client is None:
            logger.error("Discord client is not started")
            return None
        try:
            user = await self.client.fetch_user(int(message.platform_user_id))
            sent = await user.send(message.text or "")
            return SentMessageRef(platform_message_id=str(sent.id))
        except Exception as error:
            logger.exception("Failed to send Discord DM user_id=%s: %s", message.platform_user_id, error)
            return None

    @staticmethod
    def build_incoming(message: Any) -> IncomingMessage:
        attachments = [DiscordChannel.convert_attachment(attachment) for attachment in message.attachments]
        return IncomingMessage(
            platform=Platform.DISCORD.value,
            platform_user_id=str(message.author.id),
            username=str(message.author),
            full_name=getattr(message.author, "display_name", None) or str(message.author),
            text=message.content or None,
            attachments=attachments,
            raw_message_id=str(message.id),
        )

    @staticmethod
    def convert_attachment(attachment: Any) -> Attachment:
        content_type = getattr(attachment, "content_type", None) or ""
        filename = getattr(attachment, "filename", None)
        if content_type.startswith("image/"):
            attachment_type = ATTACHMENT_PHOTO
        elif content_type.startswith("video/"):
            attachment_type = ATTACHMENT_VIDEO
        else:
            attachment_type = ATTACHMENT_DOCUMENT
        return Attachment(
            type=attachment_type,
            file_url=getattr(attachment, "url", None),
            filename=filename,
            mime_type=content_type or None,
            size=getattr(attachment, "size", None),
        )
