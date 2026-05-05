from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Any

from app.bot.channels.base import ATTACHMENT_DOCUMENT, ATTACHMENT_PHOTO, ATTACHMENT_VIDEO, Attachment, IncomingMessage, OutgoingMessage, SentMessageRef
from app.bot.config import Settings
from app.bot.database.models import Platform
from app.bot.services.external_support import ExternalSupportProcessor


logger = logging.getLogger(__name__)


class VKChannel:
    platform = Platform.VK.value

    def __init__(self, settings: Settings, processor: ExternalSupportProcessor) -> None:
        self.settings = settings
        self.processor = processor
        self.vk_session: Any | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stopped = threading.Event()

    async def start(self) -> None:
        if not self.settings.vk_enabled or not self.settings.vk_longpoll_enabled:
            return
        try:
            import vk_api
            from vk_api.bot_longpoll import VkBotEventType, VkBotLongPoll
        except ImportError as error:
            logger.error("vk_api is not installed; VK channel is disabled")
            raise RuntimeError("vk_api is not installed") from error
        if not self.settings.vk_group_token or not self.settings.vk_group_id:
            logger.error("VK channel is enabled, but VK_GROUP_TOKEN or VK_GROUP_ID is empty")
            raise RuntimeError("VK_GROUP_TOKEN or VK_GROUP_ID is empty")

        self.loop = asyncio.get_running_loop()
        self.vk_session = vk_api.VkApi(token=self.settings.vk_group_token)
        longpoll = VkBotLongPoll(self.vk_session, int(self.settings.vk_group_id))
        logger.info("VK long poll started group_id=%s", self.settings.vk_group_id)

        def run_blocking() -> None:
            for event in longpoll.listen():
                if self._stopped.is_set():
                    return
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                incoming = self.build_incoming(event.object.message)
                asyncio.run_coroutine_threadsafe(self.processor.handle_incoming(incoming), self.loop)

        try:
            await asyncio.to_thread(run_blocking)
        except Exception as error:
            logger.exception("VK channel stopped with error: %s", error)
            raise

    async def stop(self) -> None:
        self._stopped.set()

    async def send_message(self, message: OutgoingMessage) -> SentMessageRef | None:
        return await self._send_vk_message(message.platform_user_id, message.text or "")

    async def send_form_menu(self, user: Any, forms: list[Any], text: str | None = None) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_form_menu_text(text),
            keyboard=vk_ui.build_form_keyboard(forms),
        )

    async def send_question(
        self,
        user: Any,
        form: Any,
        question_index: int,
        prefix_text: str | None = None,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        question = form.questions[question_index]
        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_question_text(form, question_index, prefix_text=prefix_text),
            keyboard=vk_ui.build_question_keyboard(question),
        )

    async def send_media_continue(
        self,
        user: Any,
        question_index: int,
        media_count: int,
        max_files: int | None,
        *,
        limit_reached: bool = False,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_media_continue_text(media_count, max_files, limit_reached=limit_reached),
            keyboard=vk_ui.build_media_continue_keyboard(),
        )

    async def send_minecraft_nickname_offer(
        self,
        user: Any,
        question_index: int,
        nickname: str,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_minecraft_nickname_text(nickname),
            keyboard=vk_ui.build_minecraft_nickname_keyboard(),
        )

    async def send_minecraft_lookup_confirmation(
        self,
        user: Any,
        question_index: int,
        nickname: str,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_minecraft_lookup_not_found_text(nickname),
            keyboard=vk_ui.build_minecraft_lookup_confirmation_keyboard(),
        )

    async def send_ticket_preview(self, user: Any, form: Any, answers: list[dict[str, Any]]) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_preview_text(form, answers),
            keyboard=vk_ui.build_preview_keyboard(),
        )

    async def send_ticket_sent(
        self,
        user: Any,
        ticket_id: int | None = None,
        success_text: str | None = None,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(user.platform_user_id, vk_ui.build_ticket_sent_text(ticket_id, success_text))

    async def send_closed_ticket_menu(self, user: Any, text: str, forms: list[Any]) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            text,
            keyboard=vk_ui.build_closed_ticket_keyboard(forms),
        )

    async def _send_vk_message(
        self,
        platform_user_id: str,
        text: str,
        *,
        keyboard: str | None = None,
    ) -> SentMessageRef | None:
        if self.vk_session is None:
            logger.error("VK session is not started")
            return None
        try:
            vk = self.vk_session.get_api()
            payload: dict[str, Any] = {
                "user_id": int(platform_user_id),
                "message": text or "",
                "random_id": random.randint(1, 2_147_483_647),
            }
            if keyboard is not None:
                payload["keyboard"] = keyboard
            response = vk.messages.send(**payload)
            return SentMessageRef(platform_message_id=str(response))
        except Exception as error:
            logger.exception("Failed to send VK message user_id=%s: %s", platform_user_id, error)
            return None

    @staticmethod
    def build_incoming(message: dict[str, Any]) -> IncomingMessage:
        attachments = [VKChannel.convert_attachment(attachment) for attachment in message.get("attachments", [])]
        user_id = str(message.get("from_id"))
        return IncomingMessage(
            platform=Platform.VK.value,
            platform_user_id=user_id,
            username=None,
            full_name=user_id,
            text=message.get("text") or None,
            attachments=[attachment for attachment in attachments if attachment is not None],
            raw_message_id=str(message.get("id") or message.get("conversation_message_id") or ""),
        )

    @staticmethod
    def convert_attachment(attachment: dict[str, Any]) -> Attachment | None:
        attachment_type = attachment.get("type")
        data = attachment.get(attachment_type or "", {})
        if attachment_type == "photo":
            sizes = data.get("sizes") or []
            largest = max(sizes, key=lambda item: item.get("width", 0) * item.get("height", 0), default={})
            return Attachment(type=ATTACHMENT_PHOTO, file_url=largest.get("url"))
        if attachment_type == "doc":
            return Attachment(
                type=ATTACHMENT_DOCUMENT,
                file_url=data.get("url"),
                filename=data.get("title"),
                size=data.get("size"),
            )
        if attachment_type == "video":
            owner_id = data.get("owner_id")
            video_id = data.get("id")
            access_key = data.get("access_key")
            file_url = None
            if owner_id is not None and video_id is not None:
                file_url = f"https://vk.com/video{owner_id}_{video_id}"
                if access_key:
                    file_url = f"{file_url}_{access_key}"
            return Attachment(
                type=ATTACHMENT_VIDEO,
                file_url=file_url,
                filename=data.get("title"),
            )
        return None
