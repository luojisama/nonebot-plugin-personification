from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_DEFAULT_TIMEZONE = "Asia/Shanghai"
_configured_timezone_name = _DEFAULT_TIMEZONE


def _resolve_timezone(timezone_name: str):
    if ZoneInfo is None:
        if timezone_name == "Asia/Shanghai":
            return timezone(timedelta(hours=8))
        return timezone(timedelta(hours=9))
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(_DEFAULT_TIMEZONE)


_CONFIGURED_TZ = _resolve_timezone(_configured_timezone_name)


def init_schedule_config(plugin_config: Any | None = None) -> None:
    """
    统一初始化插件时间语义。

    所有 schedule/prompt/时间工具都应读取同一个配置时区，
    避免一部分逻辑走配置，一部分逻辑仍写死东京时间。
    """
    global _configured_timezone_name, _CONFIGURED_TZ
    configured = ""
    if plugin_config is not None:
        configured = str(getattr(plugin_config, "personification_timezone", "") or "").strip()
    _configured_timezone_name = configured or _DEFAULT_TIMEZONE
    _CONFIGURED_TZ = _resolve_timezone(_configured_timezone_name)


def get_configured_timezone_name() -> str:
    return _configured_timezone_name


def get_current_local_time() -> datetime:
    return datetime.now(_CONFIGURED_TZ)


def format_time_context(now: datetime | None = None) -> str:
    target = now or get_current_local_time()
    tz_name = target.tzname() or get_configured_timezone_name()
    return f"{get_configured_timezone_name()} ({tz_name})"


def get_beijing_time() -> datetime:
    # DEPRECATED: 请使用 get_current_local_time()
    return get_current_local_time()


def get_activity_status() -> str:
    """根据当前配置时区的本地时间，返回通用时段状态。"""
    now = get_current_local_time()
    hour = now.hour
    weekday = now.weekday()
    is_weekend = weekday >= 5
    if 0 <= hour < 6:
        return "深夜到凌晨，正常应该在休息或准备睡觉。"
    if 6 <= hour < 8:
        return "清晨，刚起床或正在准备出门。"
    if 8 <= hour < 12:
        if is_weekend:
            return "周末上午，可能还在慢慢醒神，或者轻松安排自己的事。"
        return "工作日上午，通常在上学、上班或处理白天事务。"
    if 12 <= hour < 14:
        return "中午，适合吃饭、午休，也比较适合轻松聊天。"
    if 14 <= hour < 17:
        if is_weekend:
            return "周末下午，通常是自由活动时间。"
        return "工作日下午，通常还在上学、上班或外出忙事情。"
    if 17 <= hour < 19:
        return "傍晚，下班或放学时段，可能在回家路上或刚到家。"
    if 19 <= hour < 22:
        return "晚上，通常在家休息、吃饭、娱乐或处理自己的事。"
    return "夜深了，应该准备休息，或者已经在熬夜。"


def get_schedule_prompt_injection() -> str:
    now = get_current_local_time()
    status = get_activity_status()
    return (
        "## 当前时间与状态参考\n"
        f"- 当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')} [{format_time_context(now)}]\n"
        f"- 时段背景：{status}\n"
        "- 用途：帮助回复保持真实的时间感和生活气，不要把时段设定当成主题。\n"
        "- 约束：时段背景只是轻量参考，不能压过眼前正在聊的话题。\n"
        "- 如果决定回复，优先顺着对话自然接话，再少量带出时间感。\n"
    )


def is_rest_time(allow_unsuitable_prob: float = 0.0) -> bool:
    now = get_current_local_time()
    hour = now.hour
    weekday = now.weekday()

    is_rest = False
    if weekday >= 5:
        if 9 <= hour < 22:
            is_rest = True
    else:
        if 12 <= hour < 13:
            is_rest = True
        elif 18 <= hour < 23:
            is_rest = True

    if is_rest:
        return True
    if allow_unsuitable_prob > 0:
        import random

        if random.random() < allow_unsuitable_prob:
            return True
    return False


def is_group_active_hour(
    quiet_start: int = 0,
    quiet_end: int = 7,
) -> bool:
    now = get_current_local_time()
    hour = now.hour
    start = int(quiet_start)
    end = int(quiet_end)
    if start < end:
        return not (start <= hour < end)
    if start > end:
        return not (hour >= start or hour < end)
    return True
