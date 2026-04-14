from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

_configured_timezone_name = "Asia/Shanghai"
_configured_timezone = ZoneInfo(_configured_timezone_name) if ZoneInfo is not None else timezone(timedelta(hours=8))


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


def get_tokyo_now() -> datetime:
    # DEPRECATED: 请使用 get_configured_now()
    return get_configured_now()


def get_tokyo_today_str(fmt: str = "%Y-%m-%d") -> str:
    return get_current_day_str(fmt)


def format_tokyo_time(dt: datetime | None = None) -> str:
    # DEPRECATED: 请使用 format_configured_time()
    return format_configured_time(dt)
