from datetime import datetime, timedelta, timezone

TOKYO_TZ = timezone(timedelta(hours=9))


def get_tokyo_now() -> datetime:
    return datetime.now(TOKYO_TZ)


def get_tokyo_today_str(fmt: str = "%Y-%m-%d") -> str:
    return get_tokyo_now().strftime(fmt)


def format_tokyo_time(dt: datetime | None = None) -> str:
    target = dt or get_tokyo_now()
    return target.strftime("%Y-%m-%d %H:%M:%S [JST/UTC+9]")
