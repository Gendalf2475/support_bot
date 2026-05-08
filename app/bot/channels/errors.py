from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass


ERROR_TEMPORARY_NETWORK = "temporary_network_error"
ERROR_AUTH = "auth_error"
ERROR_PERMISSION = "permission_error"
ERROR_INVALID_RESPONSE = "invalid_response"
ERROR_INTENTS = "intents_error"
ERROR_USER_DM_FORBIDDEN = "user_dm_forbidden"
ERROR_POLLING_CONFLICT = "polling_conflict"
ERROR_UNEXPECTED = "unexpected_error"


@dataclass(frozen=True)
class ChannelErrorInfo:
    error_type: str
    is_temporary: bool = False
    is_user_delivery_error: bool = False


def classify_channel_error(channel_name: str, error: BaseException) -> ChannelErrorInfo:
    if channel_name == "vk":
        return classify_vk_error(error)
    if channel_name == "discord":
        return classify_discord_error(error)
    if channel_name == "telegram":
        return classify_telegram_error(error)
    return ChannelErrorInfo(ERROR_UNEXPECTED)


def classify_vk_error(error: BaseException) -> ChannelErrorInfo:
    if is_vk_temporary_network_error(error):
        return ChannelErrorInfo(ERROR_TEMPORARY_NETWORK, is_temporary=True)

    code = get_vk_error_code(error)
    if code == 27:
        return ChannelErrorInfo(ERROR_AUTH)
    if code == 15:
        return ChannelErrorInfo(ERROR_PERMISSION)
    if code in {901, 902}:
        return ChannelErrorInfo(ERROR_USER_DM_FORBIDDEN, is_user_delivery_error=True)
    if is_json_decode_error(error):
        return ChannelErrorInfo(ERROR_INVALID_RESPONSE, is_temporary=True)

    return ChannelErrorInfo(ERROR_UNEXPECTED)


def classify_discord_error(error: BaseException) -> ChannelErrorInfo:
    error_name = type(error).__name__.casefold()
    normalized = _normalized_error(error)

    if "privilegedintentsrequired" in error_name or "privileged intents" in normalized:
        return ChannelErrorInfo(ERROR_INTENTS)
    if "loginfailure" in error_name or "improper token" in normalized or "invalid token" in normalized:
        return ChannelErrorInfo(ERROR_AUTH)
    if "forbidden" in error_name:
        return ChannelErrorInfo(ERROR_USER_DM_FORBIDDEN, is_user_delivery_error=True)
    if any(marker in normalized for marker in ("gateway", "websocket", "connectionclosed", "disconnect", "connection reset")):
        return ChannelErrorInfo(ERROR_TEMPORARY_NETWORK, is_temporary=True)
    if any(marker in error_name for marker in ("connectionclosed", "gatewaynotfound")):
        return ChannelErrorInfo(ERROR_TEMPORARY_NETWORK, is_temporary=True)

    return ChannelErrorInfo(ERROR_UNEXPECTED)


def classify_telegram_error(error: BaseException) -> ChannelErrorInfo:
    error_name = type(error).__name__.casefold()
    normalized = _normalized_error(error)

    if "network" in error_name or "timeout" in normalized or "timed out" in normalized:
        return ChannelErrorInfo(ERROR_TEMPORARY_NETWORK, is_temporary=True)
    if "unauthorized" in error_name or "invalid token" in normalized or "unauthorized" in normalized:
        return ChannelErrorInfo(ERROR_AUTH)
    if "conflict" in error_name or "terminated by other getupdates request" in normalized:
        return ChannelErrorInfo(ERROR_POLLING_CONFLICT)
    if "forbidden" in error_name and "bot was blocked" in normalized:
        return ChannelErrorInfo(ERROR_USER_DM_FORBIDDEN, is_user_delivery_error=True)

    return ChannelErrorInfo(ERROR_UNEXPECTED)


def is_vk_temporary_network_error(error: BaseException) -> bool:
    return is_vk_timeout_error(error) or is_vk_connection_error(error)


def is_vk_timeout_error(error: BaseException) -> bool:
    return _is_instance(error, "requests.exceptions", ("ReadTimeout", "Timeout")) or _is_instance(
        error,
        "urllib3.exceptions",
        ("ReadTimeoutError",),
    )


def is_vk_connection_error(error: BaseException) -> bool:
    return _is_instance(error, "requests.exceptions", ("ConnectionError",))


def is_vk_read_timeout(error: BaseException) -> bool:
    return _is_instance(error, "requests.exceptions", ("ReadTimeout",)) or _is_instance(
        error,
        "urllib3.exceptions",
        ("ReadTimeoutError",),
    )


def is_json_decode_error(error: BaseException) -> bool:
    return isinstance(error, json.JSONDecodeError) or _is_instance(error, "requests.exceptions", ("JSONDecodeError",))


def is_user_delivery_error(channel_name: str, error: BaseException) -> bool:
    return classify_channel_error(channel_name, error).is_user_delivery_error


def get_vk_error_code(error: BaseException) -> int | None:
    for attr_name in ("code", "error_code"):
        code = _parse_int(getattr(error, attr_name, None))
        if code is not None:
            return code

    for attr_name in ("error", "data"):
        raw_data = getattr(error, attr_name, None)
        if isinstance(raw_data, dict):
            code = _parse_int(raw_data.get("error_code") or raw_data.get("code"))
            if code is not None:
                return code

    match = re.search(r"\[(\d+)]", str(error))
    if match:
        return _parse_int(match.group(1))

    match = re.search(r"error[_ ]?code['\"]?\s*[:=]\s*(\d+)", str(error), flags=re.IGNORECASE)
    if match:
        return _parse_int(match.group(1))

    return None


def _is_instance(error: BaseException, module_name: str, class_names: tuple[str, ...]) -> bool:
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return False

    classes = tuple(
        candidate
        for class_name in class_names
        if isinstance((candidate := getattr(module, class_name, None)), type)
    )
    return bool(classes) and isinstance(error, classes)


def _normalized_error(error: BaseException) -> str:
    return f"{type(error).__name__} {error}".casefold()


def _parse_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
