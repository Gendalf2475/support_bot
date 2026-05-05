from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from app.bot.config import Settings
from app.bot.services.ticket_form_service import DEFAULT_MINECRAFT_NICKNAME_REGEX


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlayerLookupResult:
    success: bool
    exists: bool | None
    nickname: str | None
    uuid: str | None
    online: bool | None
    source: str | None
    error: str | None


class MinecraftService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        if self.is_enabled():
            logger.info("Minecraft API enabled type=%s base_url=%s", self.settings.minecraft_api_type, self._base_url())
        else:
            logger.info("Minecraft API disabled")

    def is_enabled(self) -> bool:
        return bool(self.settings.minecraft_api_enabled) and self._api_type() == "http"

    async def check_player(self, nickname: str, *, respect_check_enabled: bool = True) -> PlayerLookupResult:
        normalized_nickname = str(nickname or "").strip()
        if not self.is_enabled():
            return self._result(False, None, normalized_nickname or None, error="disabled")
        if respect_check_enabled and not self.settings.minecraft_nickname_check_enabled:
            return self._result(False, None, normalized_nickname or None, error="disabled")
        if not re.fullmatch(DEFAULT_MINECRAFT_NICKNAME_REGEX, normalized_nickname):
            return self._result(False, None, normalized_nickname or None, error="invalid_nickname")
        if self._api_type() != "http":
            return self._result(False, None, normalized_nickname, error="unsupported_type")

        logger.info("Minecraft player lookup requested nickname=%s", normalized_nickname)
        url = f"{self._base_url()}/player/{quote(normalized_nickname, safe='')}"
        headers = self._auth_headers()
        timeout = max(0.1, float(self.settings.minecraft_http_api_timeout or 5))

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            logger.warning("Minecraft player lookup failed nickname=%s error=timeout", normalized_nickname)
            return self._result(False, None, normalized_nickname, error="timeout")
        except httpx.RequestError as error:
            logger.warning(
                "Minecraft player lookup failed nickname=%s error=connection_error detail=%s",
                normalized_nickname,
                type(error).__name__,
            )
            return self._result(False, None, normalized_nickname, error="connection_error")

        if response.status_code != 200:
            error_code = self._http_error(response.status_code)
            logger.warning(
                "Minecraft player lookup failed nickname=%s status=%s error=%s",
                normalized_nickname,
                response.status_code,
                error_code,
            )
            return self._result(False, None, normalized_nickname, error=error_code)

        try:
            data = response.json()
        except ValueError:
            logger.warning("Minecraft player lookup failed nickname=%s error=invalid_response", normalized_nickname)
            return self._result(False, None, normalized_nickname, error="invalid_response")
        if not isinstance(data, dict):
            logger.warning("Minecraft player lookup failed nickname=%s error=invalid_response", normalized_nickname)
            return self._result(False, None, normalized_nickname, error="invalid_response")

        result = self._parse_response(data, normalized_nickname)
        if result.success:
            logger.info(
                "Minecraft player lookup result nickname=%s exists=%s source=%s",
                result.nickname or normalized_nickname,
                result.exists,
                result.source,
            )
        else:
            logger.warning(
                "Minecraft player lookup failed nickname=%s error=%s",
                result.nickname or normalized_nickname,
                result.error,
            )
        return result

    def _parse_response(self, data: dict[str, Any], fallback_nickname: str) -> PlayerLookupResult:
        success = bool(data.get("success"))
        exists = data.get("exists")
        if exists is not None:
            exists = bool(exists)
        online = data.get("online")
        if online is not None:
            online = bool(online)
        nickname = str(data.get("nickname") or fallback_nickname).strip() or fallback_nickname
        uuid = str(data.get("uuid") or "").strip() or None
        source = str(data.get("source") or "").strip() or None
        error = str(data.get("error") or "").strip() or None
        return PlayerLookupResult(
            success=success,
            exists=exists,
            nickname=nickname,
            uuid=uuid,
            online=online,
            source=source,
            error=error,
        )

    def _auth_headers(self) -> dict[str, str]:
        token = str(self.settings.minecraft_http_api_token or "").strip()
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _base_url(self) -> str:
        return str(self.settings.minecraft_http_api_base_url or "").strip().rstrip("/") or "http://127.0.0.1:8085"

    def _api_type(self) -> str:
        return str(self.settings.minecraft_api_type or "http").strip().lower()

    @staticmethod
    def _result(
        success: bool,
        exists: bool | None,
        nickname: str | None,
        *,
        uuid: str | None = None,
        online: bool | None = None,
        source: str | None = None,
        error: str | None = None,
    ) -> PlayerLookupResult:
        return PlayerLookupResult(
            success=success,
            exists=exists,
            nickname=nickname,
            uuid=uuid,
            online=online,
            source=source,
            error=error,
        )

    @staticmethod
    def _http_error(status_code: int) -> str:
        if status_code == 400:
            return "invalid_nickname"
        if status_code == 401:
            return "unauthorized"
        if status_code == 403:
            return "forbidden"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "api_error"
        return "api_error"


def player_lookup_to_dict(result: PlayerLookupResult) -> dict[str, Any]:
    return {
        "success": result.success,
        "exists": result.exists,
        "nickname": result.nickname,
        "uuid": result.uuid,
        "online": result.online,
        "source": result.source,
        "error": result.error,
    }
