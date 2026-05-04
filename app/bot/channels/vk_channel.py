from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Any

from app.bot.channels.base import ATTACHMENT_DOCUMENT, ATTACHMENT_PHOTO, Attachment, IncomingMessage, OutgoingMessage, SentMessageRef
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
        except ImportError:
            logger.error("vk_api is not installed; VK channel is disabled")
            return
        if not self.settings.vk_group_token or not self.settings.vk_group_id:
            logger.error("VK channel is enabled, but VK_GROUP_TOKEN or VK_GROUP_ID is empty")
            return

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

    async def stop(self) -> None:
        self._stopped.set()

    async def send_message(self, message: OutgoingMessage) -> SentMessageRef | None:
        if self.vk_session is None:
            logger.error("VK session is not started")
            return None
        try:
            vk = self.vk_session.get_api()
            response = vk.messages.send(
                user_id=int(message.platform_user_id),
                message=message.text or "",
                random_id=random.randint(1, 2_147_483_647),
            )
            return SentMessageRef(platform_message_id=str(response))
        except Exception as error:
            logger.exception("Failed to send VK message user_id=%s: %s", message.platform_user_id, error)
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
        return None
