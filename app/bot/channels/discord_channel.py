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
        except ImportError as error:
            logger.error("discord.py is not installed; Discord channel is disabled")
            raise RuntimeError("discord.py is not installed") from error
        if not self.settings.discord_bot_token:
            logger.error("Discord channel is enabled, but DISCORD_BOT_TOKEN is empty")
            raise RuntimeError("DISCORD_BOT_TOKEN is empty")

        intents = discord.Intents.default()
        intents.dm_messages = True
        intents.message_content = True
        activity = self._build_activity(discord)
        client = discord.Client(intents=intents, status=discord.Status.online, activity=activity)
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
            raise

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

    async def send_form_menu(self, user: Any, forms: list[Any], text: str | None = None) -> SentMessageRef | None:
        if not forms:
            return await self.send_message(
                OutgoingMessage(
                    platform=Platform.DISCORD.value,
                    platform_user_id=user.platform_user_id,
                    text=text or "Список форм сейчас недоступен. Попробуйте позже.",
                )
            )
        discord_ui = self._discord_ui()
        embed = discord_ui.build_main_menu_embed()
        if text:
            embed.description = f"{text.strip()}\n\n{embed.description}"
        view = discord_ui.FormSelectView(
            forms=forms,
            owner_id=int(user.platform_user_id),
            on_select=self._handle_form_select,
        )
        return await self._send_discord_dm(user.platform_user_id, embed=embed, view=view)

    async def send_question(
        self,
        user: Any,
        form: Any,
        question_index: int,
        prefix_text: str | None = None,
    ) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        question = form.questions[question_index]
        embed = discord_ui.build_question_embed(form, question_index, prefix_text=prefix_text)
        view = discord_ui.QuestionActionView(
            question=question,
            question_index=question_index,
            owner_id=int(user.platform_user_id),
            on_action=self._handle_action,
        )
        return await self._send_discord_dm(user.platform_user_id, embed=embed, view=view)

    async def send_media_continue(
        self,
        user: Any,
        question_index: int,
        media_count: int,
        max_files: int | None,
        *,
        limit_reached: bool = False,
    ) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        embed = discord_ui.build_media_continue_embed(media_count, max_files, limit_reached=limit_reached)
        view = discord_ui.MediaContinueView(
            question_index=question_index,
            owner_id=int(user.platform_user_id),
            on_action=self._handle_action,
        )
        return await self._send_discord_dm(user.platform_user_id, embed=embed, view=view)

    async def send_minecraft_nickname_offer(
        self,
        user: Any,
        question_index: int,
        nickname: str,
    ) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        embed = discord_ui.build_minecraft_nickname_embed(nickname)
        view = discord_ui.MinecraftNicknameView(
            question_index=question_index,
            owner_id=int(user.platform_user_id),
            on_action=self._handle_action,
        )
        return await self._send_discord_dm(user.platform_user_id, embed=embed, view=view)

    async def send_ticket_preview(self, user: Any, form: Any, answers: list[dict[str, Any]]) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        embed = discord_ui.build_ticket_preview_embed(form, answers)
        view = discord_ui.TicketPreviewView(
            form_id=form.id,
            owner_id=int(user.platform_user_id),
            on_action=self._handle_action,
        )
        return await self._send_discord_dm(user.platform_user_id, embed=embed, view=view)

    async def send_ticket_sent(
        self,
        user: Any,
        ticket_id: int | None = None,
        success_text: str | None = None,
    ) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        embed = discord_ui.build_ticket_sent_embed(ticket_id, success_text)
        return await self._send_discord_dm(user.platform_user_id, embed=embed)

    async def send_closed_ticket_menu(self, user: Any, text: str, forms: list[Any]) -> SentMessageRef | None:
        discord_ui = self._discord_ui()
        if not forms:
            return await self._send_discord_dm(user.platform_user_id, embed=discord_ui.build_closed_ticket_embed(text))
        view = discord_ui.ClosedTicketView(
            forms=forms,
            owner_id=int(user.platform_user_id),
            on_select=self._handle_form_select,
        )
        return await self._send_discord_dm(
            user.platform_user_id,
            embed=discord_ui.build_closed_ticket_embed(text),
            view=view,
        )

    async def _send_discord_dm(
        self,
        platform_user_id: str,
        *,
        embed: Any | None = None,
        view: Any | None = None,
        text: str | None = None,
    ) -> SentMessageRef | None:
        if self.client is None:
            logger.error("Discord client is not started")
            return None
        try:
            user = await self.client.fetch_user(int(platform_user_id))
            sent = await user.send(content=text, embed=embed, view=view)
            return SentMessageRef(platform_message_id=str(sent.id))
        except Exception as error:
            logger.exception("Failed to send Discord UI DM user_id=%s: %s", platform_user_id, error)
            return None

    async def _handle_form_select(self, interaction: Any, form_id: str) -> None:
        await self._defer_interaction(interaction)
        username, full_name = self._interaction_user_names(interaction)
        try:
            await self.processor.handle_platform_form_selection(
                platform=Platform.DISCORD.value,
                platform_user_id=str(interaction.user.id),
                form_id=form_id,
                username=username,
                full_name=full_name,
            )
        except Exception as error:
            logger.exception("Failed to handle Discord form selection user_id=%s form_id=%s: %s", interaction.user.id, form_id, error)
            await self._send_interaction_notice(interaction, "Не удалось выполнить действие. Попробуйте ещё раз.")

    async def _handle_action(
        self,
        interaction: Any,
        action: str,
        question_index: int | None,
        form_id: str | None,
    ) -> None:
        await self._defer_interaction(interaction)
        username, full_name = self._interaction_user_names(interaction)
        try:
            await self.processor.handle_platform_action(
                platform=Platform.DISCORD.value,
                platform_user_id=str(interaction.user.id),
                action=action,
                username=username,
                full_name=full_name,
                question_index=question_index,
                form_id=form_id,
            )
        except Exception as error:
            logger.exception("Failed to handle Discord action user_id=%s action=%s: %s", interaction.user.id, action, error)
            await self._send_interaction_notice(interaction, "Не удалось выполнить действие. Попробуйте ещё раз.")

    async def _defer_interaction(self, interaction: Any) -> None:
        try:
            if not interaction.response.is_done():
                await interaction.response.defer()
        except Exception as error:
            logger.warning("Failed to defer Discord interaction user_id=%s: %s", getattr(interaction.user, "id", None), error)

    async def _send_interaction_notice(self, interaction: Any, text: str) -> None:
        discord_ui = self._discord_ui()
        await discord_ui.send_interaction_notice(interaction, text)

    @staticmethod
    def _interaction_user_names(interaction: Any) -> tuple[str | None, str | None]:
        user = interaction.user
        username = str(user) if user is not None else None
        full_name = (
            getattr(user, "global_name", None)
            or getattr(user, "display_name", None)
            or username
        )
        return username, full_name

    @staticmethod
    def _discord_ui() -> Any:
        from app.bot.channels import discord_ui

        return discord_ui

    def _build_activity(self, discord: Any) -> Any | None:
        activity_name = str(self.settings.discord_activity_name or "").strip()
        if not self.settings.discord_activity_enabled or not activity_name:
            logger.info("Discord activity disabled")
            return None

        activity_type = str(self.settings.discord_activity_type or "playing").strip().lower()
        if activity_type == "playing":
            activity = discord.Game(name=activity_name)
        elif activity_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=activity_name)
        elif activity_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=activity_name)
        elif activity_type == "competing":
            activity = discord.Activity(type=discord.ActivityType.competing, name=activity_name)
        else:
            logger.warning("Unknown Discord activity type: %s, fallback to playing", activity_type)
            activity_type = "playing"
            activity = discord.Game(name=activity_name)

        logger.info("Discord activity set: %s %s", activity_type, activity_name)
        return activity

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
