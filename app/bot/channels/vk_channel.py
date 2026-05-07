from __future__ import annotations

import asyncio
import logging
import random
import threading
from typing import Any

from app.bot.channels.base import ATTACHMENT_DOCUMENT, ATTACHMENT_PHOTO, ATTACHMENT_VIDEO, Attachment, IncomingMessage, OutgoingMessage, SentMessageRef
from app.bot.channels.errors import ERROR_TEMPORARY_NETWORK, classify_vk_error, is_user_delivery_error, is_vk_read_timeout
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
        self.failure_notifier: Any | None = None
        self.health_registry: Any | None = None
        self._network_error_count = 0
        self._reconnect_attempt = 0
        self._reconnect_delay = 0
        self._restore_notification_scheduled = False

    def set_supervision(self, *, failure_notifier: Any | None = None, health_registry: Any | None = None) -> None:
        self.failure_notifier = failure_notifier
        self.health_registry = health_registry

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

        self._stopped.clear()
        self.loop = asyncio.get_running_loop()
        self.vk_session = vk_api.VkApi(token=self.settings.vk_group_token)
        self._network_error_count = 0
        self._reconnect_attempt = 0
        self._reconnect_delay = self._vk_reconnect_delay()
        self._restore_notification_scheduled = False

        while not self._stopped.is_set():
            try:
                longpoll = VkBotLongPoll(self.vk_session, int(self.settings.vk_group_id))
                logger.info("VK long poll started group_id=%s", self.settings.vk_group_id)
                if not self._network_error_count:
                    self._mark_working()
                await asyncio.to_thread(self._listen_blocking, longpoll, VkBotEventType)
                if self._stopped.is_set():
                    return
                raise RuntimeError("VK long poll exited unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                info = classify_vk_error(error)
                if info.error_type != ERROR_TEMPORARY_NETWORK or not self.settings.vk_reconnect_enabled:
                    raise

                self._network_error_count += 1
                self._reconnect_attempt += 1
                current_delay = self._reconnect_delay
                self._mark_reconnecting(error, self._network_error_count)
                if is_vk_read_timeout(error):
                    logger.warning(
                        "VK Long Poll timeout, reconnecting attempt=%s delay=%s",
                        self._reconnect_attempt,
                        current_delay,
                    )
                else:
                    logger.warning(
                        "VK Long Poll network error, reconnecting attempt=%s delay=%s error=%s",
                        self._reconnect_attempt,
                        current_delay,
                        error,
                    )
                await self._notify_timeout_instability(self._network_error_count)
                await asyncio.sleep(current_delay)
                self._reconnect_delay = min(
                    max(current_delay * 2, self._vk_reconnect_delay()),
                    self._vk_reconnect_max_delay(),
                )

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
        *,
        change_label: str = "Ввести другой",
        text: str | None = None,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            text or vk_ui.build_minecraft_nickname_text(nickname),
            keyboard=vk_ui.build_minecraft_nickname_keyboard(change_label=change_label),
        )

    async def send_minecraft_nickname_change_confirmation(
        self,
        user: Any,
        question_index: int,
        nickname: str,
    ) -> SentMessageRef | None:
        from app.bot.channels import vk_ui

        return await self._send_vk_message(
            user.platform_user_id,
            vk_ui.build_minecraft_nickname_change_confirmation_text(nickname),
            keyboard=vk_ui.build_minecraft_nickname_change_confirm_keyboard(),
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
            if is_user_delivery_error(Platform.VK.value, error):
                logger.warning("Failed to deliver VK message user_id=%s: %s", platform_user_id, error)
            else:
                logger.exception("Failed to send VK message user_id=%s: %s", platform_user_id, error)
            return None

    def _listen_blocking(self, longpoll: Any, event_type: Any) -> None:
        for event in longpoll.listen():
            if self._stopped.is_set():
                return
            self._schedule_restored_if_needed()
            if event.type != event_type.MESSAGE_NEW:
                continue
            incoming = self.build_incoming(event.object.message)
            if self.loop is None:
                logger.error("VK event loop is not available")
                continue
            future = asyncio.run_coroutine_threadsafe(self.processor.handle_incoming(incoming), self.loop)
            future.add_done_callback(self._log_incoming_error)

    @staticmethod
    def _log_incoming_error(future: Any) -> None:
        try:
            future.result()
        except Exception as error:
            logger.exception("Failed to handle VK incoming message: %s", error)

    def _vk_reconnect_delay(self) -> int:
        return max(0, self.settings.vk_reconnect_delay_seconds)

    def _vk_reconnect_max_delay(self) -> int:
        return max(self._vk_reconnect_delay(), self.settings.vk_reconnect_max_delay_seconds)

    def _mark_working(self) -> None:
        if self.health_registry is not None:
            self.health_registry.mark_working(Platform.VK.value)

    def _mark_reconnecting(self, error: Exception, consecutive_errors: int) -> None:
        if self.health_registry is not None:
            self.health_registry.mark_reconnecting(
                Platform.VK.value,
                error_type=ERROR_TEMPORARY_NETWORK,
                error=error,
                consecutive_errors=consecutive_errors,
            )

    async def _notify_timeout_instability(self, consecutive_errors: int) -> None:
        notify_after = max(1, self.settings.vk_timeout_notify_after_failures)
        if consecutive_errors < notify_after or self.failure_notifier is None:
            return
        await self.failure_notifier.notify_failure(
            Platform.VK.value,
            ERROR_TEMPORARY_NETWORK,
            text="⚠️ VK-канал временно нестабилен: проблемы соединения с VK Long Poll. Бот продолжает переподключаться.",
        )

    def _schedule_restored_if_needed(self) -> None:
        if self._network_error_count <= 0 or self.loop is None or self._restore_notification_scheduled:
            return
        self._restore_notification_scheduled = True
        asyncio.run_coroutine_threadsafe(self._notify_restored(), self.loop)

    async def _notify_restored(self) -> None:
        had_network_errors = self._network_error_count > 0
        self._network_error_count = 0
        self._reconnect_attempt = 0
        self._reconnect_delay = self._vk_reconnect_delay()
        self._restore_notification_scheduled = False
        if not had_network_errors:
            return
        if self.health_registry is not None:
            self.health_registry.mark_restored(Platform.VK.value)
        logger.info("Channel restored channel=vk")
        if self.failure_notifier is not None:
            await self.failure_notifier.notify_restored(Platform.VK.value)

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
