from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_configured_timezone_name = "Asia/Shanghai"
_configured_timezone = ZoneInfo(_configured_timezone_name) if ZoneInfo is not None else timezone(timedelta(hours=8))
_TIME_CONTEXT_MARKER = "[personification:current_time_context]"


def init_time_context(timezone_name: str = "Asia/Shanghai") -> None:
    global _configured_timezone_name, _configured_timezone
    _configured_timezone_name = str(timezone_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if ZoneInfo is not None:
        try:
            _configured_timezone = ZoneInfo(_configured_timezone_name)
            return
        except Exception:
            _configured_timezone_name = "Asia/Shanghai"
            _configured_timezone = ZoneInfo(_configured_timezone_name)
            return
    _configured_timezone = timezone(timedelta(hours=8))


def get_configured_now() -> datetime:
    return datetime.now(_configured_timezone)


def get_configured_timezone_name() -> str:
    return _configured_timezone_name


def get_current_day_str(fmt: str = "%Y-%m-%d") -> str:
    return get_configured_now().strftime(fmt)


def format_configured_time(dt: datetime | None = None) -> str:
    target = dt or get_configured_now()
    tz_name = target.tzname() or _configured_timezone_name
    return target.strftime("%Y-%m-%d %H:%M:%S") + f" [{_configured_timezone_name}/{tz_name}]"


def get_current_time() -> datetime:
    return get_configured_now()


def format_current_time(dt: datetime | None = None) -> str:
    return format_configured_time(dt)


def _weekday_label(now: datetime) -> str:
    labels = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
    return labels[now.weekday()]


def time_period_label(now: datetime | None = None) -> str:
    current = now or get_configured_now()
    hour = current.hour
    if 0 <= hour < 6:
        return "深夜/凌晨"
    if 6 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 22:
        return "晚上"
    return "夜间"


def build_current_time_context_block(now: datetime | None = None) -> str:
    current = now or get_configured_now()
    tz_name = current.tzname() or _configured_timezone_name
    return (
        f"{_TIME_CONTEXT_MARKER}\n"
        "## 当前时间信息\n"
        f"- 当前时间：{current.strftime('%Y-%m-%d %H:%M:%S')} [{_configured_timezone_name}/{tz_name}]\n"
        f"- 日期：{current.strftime('%Y-%m-%d')}，{_weekday_label(current)}\n"
        f"- 时段：{time_period_label(current)}\n"
        "- 用途：所有时间相关判断、用户画像、群聊风格、记忆整理、主动消息和工具决策都以此为准。"
    )


def _message_text_for_time_detection(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "") or ""))
                continue
            parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def messages_have_current_time_context(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if _TIME_CONTEXT_MARKER in _message_text_for_time_detection(message):
            return True
    return False


def inject_current_time_context(
    messages: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    copied = [dict(message) for message in list(messages or []) if isinstance(message, dict)]
    if messages_have_current_time_context(copied):
        return copied
    time_message = {
        "role": "system",
        "content": build_current_time_context_block(now),
    }
    return [time_message, *copied]


def get_tokyo_now() -> datetime:
    # DEPRECATED: 请使用 get_configured_now()
    return get_configured_now()


def get_tokyo_today_str(fmt: str = "%Y-%m-%d") -> str:
    return get_current_day_str(fmt)


def format_tokyo_time(dt: datetime | None = None) -> str:
    # DEPRECATED: 请使用 format_configured_time()
    return format_configured_time(dt)
