from __future__ import annotations

import functools
import traceback
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator, TypeVar


T = TypeVar("T")


def log_exception(
    logger: Any,
    context: str,
    exc: Exception,
    *,
    level: str = "warning",
    include_traceback: bool = False,
) -> None:
    if logger is None:
        return
    method = getattr(logger, level, None) or getattr(logger, "warning", None)
    if method is None:
        return
    try:
        method(f"{str(context or 'unexpected error').strip()}: {exc}")
    except Exception:
        return
    if include_traceback:
        debug = getattr(logger, "debug", None)
        if debug is not None:
            try:
                debug(traceback.format_exc())
            except Exception:
                return


def log_on_exception(
    fallback: T | None = None,
    *,
    tag: str = "",
    level: str = "warning",
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T | None]]]:
    def decorator(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T | None]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T | None:
            try:
                return await func(*args, **kwargs)
            except Exception as exc:
                active_logger = kwargs.get("logger")
                if active_logger is None and args:
                    active_logger = getattr(args[0], "logger", None)
                log_exception(
                    active_logger,
                    tag or func.__qualname__,
                    exc,
                    level=level,
                    include_traceback=True,
                )
                return fallback

        return wrapper

    return decorator


@contextmanager
def swallow_and_log(
    logger: Any,
    context: str,
    *,
    level: str = "warning",
) -> Iterator[None]:
    try:
        yield
    except Exception as exc:
        log_exception(logger, context, exc, level=level, include_traceback=True)

__all__ = ["log_exception", "log_on_exception", "swallow_and_log"]
