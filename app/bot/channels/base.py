from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.bot.database.models import Platform, utcnow


ATTACHMENT_PHOTO = "photo"
ATTACHMENT_VIDEO = "video"
ATTACHMENT_DOCUMENT = "document"
ATTACHMENT_AUDIO = "audio"
ATTACHMENT_VOICE = "voice"
ATTACHMENT_STICKER = "sticker"
ATTACHMENT_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Attachment:
    type: str = ATTACHMENT_UNKNOWN
    file_id: str | None = None
    file_url: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    caption: str | None = None
    size: int | None = None


@dataclass(frozen=True)
class IncomingMessage:
    platform: str
    platform_user_id: str
    username: str | None = None
    full_name: str | None = None
    text: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    raw_message_id: str | None = None
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class OutgoingMessage:
    platform: str
    platform_user_id: str
    text: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    reply_to_message_id: str | None = None


@dataclass(frozen=True)
class SentMessageRef:
    platform_message_id: str | None = None


class ChannelAdapter(Protocol):
    platform: str

    async def send_message(self, message: OutgoingMessage) -> SentMessageRef | None:
        ...


def platform_display_name(platform: str | None) -> str:
    names = {
        Platform.TELEGRAM.value: "Telegram",
        Platform.DISCORD.value: "Discord",
        Platform.VK.value: "VK",
    }
    return names.get(str(platform or Platform.TELEGRAM.value), str(platform or "unknown"))
