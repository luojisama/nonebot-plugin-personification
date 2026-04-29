from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from plugin.personification.agent.tool_registry import AgentTool


DATETIME_DESCRIPTION = """获取当前精确时间，包括日期、星期、时刻、节气信息。
适合场景：用户问几点了、今天星期几、现在什么节气。
不要主动调用此工具系统 prompt 已经注入了当前时间，
只有用户明确询问时间相关信息时才调用。"""

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

SOLAR_TERMS = [
    ((1, 6), "小寒"),
    ((1, 20), "大寒"),
    ((2, 4), "立春"),
    ((2, 19), "雨水"),
    ((3, 5), "惊蛰"),
    ((3, 20), "春分"),
    ((4, 4), "清明"),
    ((4, 20), "谷雨"),
    ((5, 5), "立夏"),
    ((5, 21), "小满"),
    ((6, 5), "芒种"),
    ((6, 21), "夏至"),
    ((7, 7), "小暑"),
    ((7, 22), "大暑"),
    ((8, 7), "立秋"),
    ((8, 23), "处暑"),
    ((9, 7), "白露"),
    ((9, 23), "秋分"),
    ((10, 8), "寒露"),
    ((10, 23), "霜降"),
    ((11, 7), "立冬"),
    ((11, 22), "小雪"),
    ((12, 7), "大雪"),
    ((12, 21), "冬至"),
]


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _resolve_now(timezone_name: str, now: Optional[datetime] = None) -> datetime:
    tz = _resolve_timezone(timezone_name)
    if now is not None:
        return now.astimezone(tz)
    return datetime.now(tz)


def _time_period(hour: int) -> str:
    if 5 <= hour < 9:
        return "早晨"
    if 9 <= hour < 12:
        return "上午"
    if 12 <= hour < 14:
        return "中午"
    if 14 <= hour < 18:
        return "下午"
    if 18 <= hour < 24:
        return "晚上"
    return "凌晨"


def _solar_term(now: datetime) -> str:
    current = (now.month, now.day)
    term = SOLAR_TERMS[-1][1]
    for marker, name in SOLAR_TERMS:
        if current >= marker:
            term = name
        else:
            break
    return term


def get_current_datetime_info(
    timezone_name: str = "Asia/Shanghai",
    now: Optional[datetime] = None,
) -> str:
    current = _resolve_now(timezone_name, now)
    weekday = WEEKDAY_NAMES[current.weekday()]
    return (
        f"现在是 {current:%Y年%m月%d日} {weekday} {current:%H:%M}，"
        f"时段：{_time_period(current.hour)}，节气：{_solar_term(current)}，"
        f"时区：{timezone_name}"
    )


def build_datetime_tool(timezone_name: str = "Asia/Shanghai") -> AgentTool:
    async def _handler() -> str:
        return get_current_datetime_info(timezone_name)

    return AgentTool(
        name="datetime",
        description=DATETIME_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        handler=_handler,
    )
