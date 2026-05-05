from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    bot_token: str = Field(..., alias="BOT_TOKEN")
    support_chat_id: int = Field(..., alias="SUPPORT_CHAT_ID")
    database_url: str = Field(
        "sqlite+aiosqlite:///support_bot.db",
        alias="DATABASE_URL",
    )
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    ticket_forms_path: Path = PROJECT_ROOT / "config" / "ticket_forms.yml"
    telegram_enabled: bool = Field(True, alias="TELEGRAM_ENABLED")
    discord_enabled: bool = Field(False, alias="DISCORD_ENABLED")
    discord_bot_token: str = Field("", alias="DISCORD_BOT_TOKEN")
    discord_guild_id: str = Field("", alias="DISCORD_GUILD_ID")
    discord_support_mode: str = Field("dm", alias="DISCORD_SUPPORT_MODE")
    vk_enabled: bool = Field(False, alias="VK_ENABLED")
    vk_group_token: str = Field("", alias="VK_GROUP_TOKEN")
    vk_group_id: str = Field("", alias="VK_GROUP_ID")
    vk_longpoll_enabled: bool = Field(True, alias="VK_LONGPOLL_ENABLED")
    ticket_reminder_enabled: bool = Field(True, alias="TICKET_REMINDER_ENABLED")
    ticket_reminder_after_hours: int = Field(24, alias="TICKET_REMINDER_AFTER_HOURS")
    ticket_reminder_interval_minutes: int = Field(60, alias="TICKET_REMINDER_INTERVAL_MINUTES")
    ticket_check_interval_minutes: int = Field(10, alias="TICKET_CHECK_INTERVAL_MINUTES")
    ticket_auto_close_enabled: bool = Field(True, alias="TICKET_AUTO_CLOSE_ENABLED")
    ticket_auto_close_after_days: int = Field(7, alias="TICKET_AUTO_CLOSE_AFTER_DAYS")
    user_reply_reminder_enabled: bool = Field(True, alias="USER_REPLY_REMINDER_ENABLED")
    user_reply_reminder_after_hours: int = Field(24, alias="USER_REPLY_REMINDER_AFTER_HOURS")
    user_reply_reminder_interval_hours: int = Field(24, alias="USER_REPLY_REMINDER_INTERVAL_HOURS")
    ticket_auto_close_warning_enabled: bool = Field(True, alias="TICKET_AUTO_CLOSE_WARNING_ENABLED")
    ticket_auto_close_warning_hours: int = Field(24, alias="TICKET_AUTO_CLOSE_WARNING_HOURS")
    channel_failure_notify_enabled: bool = Field(True, alias="CHANNEL_FAILURE_NOTIFY_ENABLED")
    channel_failure_notify_cooldown_minutes: int = Field(30, alias="CHANNEL_FAILURE_NOTIFY_COOLDOWN_MINUTES")

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
