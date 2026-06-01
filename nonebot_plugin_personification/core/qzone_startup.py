from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


async def refresh_qzone_cookie_on_available_bot(
    *,
    enabled: bool,
    get_bots: Callable[[], dict[str, Any]],
    update_qzone_cookie: Callable[[Any], Awaitable[tuple[bool, str]]],
    logger: Any,
    wait_seconds: float = 60.0,
    poll_interval: float = 2.0,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> bool:
    """Refresh the Qzone cookie once after startup when a bot connection is ready."""
    if not enabled:
        return False

    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    bot = None
    while True:
        try:
            bots = get_bots() or {}
        except Exception as exc:
            bots = {}
            logger.warning(f"拟人插件：启动时读取 Bot 实例失败，暂缓刷新 Qzone Cookie：{exc}")
        if bots:
            bot = next(iter(bots.values()))
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("拟人插件：启动后未找到有效 Bot 实例，跳过 Qzone Cookie 自动刷新。")
            return False
        await sleep(min(max(0.1, float(poll_interval or 0.1)), remaining))

    try:
        ok, message = await update_qzone_cookie(bot)
    except Exception as exc:
        logger.warning(f"拟人插件：启动时 Qzone Cookie 自动刷新失败：{exc}")
        return False

    if ok:
        logger.info("拟人插件：启动时 Qzone Cookie 自动刷新成功。")
        return True

    logger.warning(f"拟人插件：启动时 Qzone Cookie 自动刷新失败：{message}")
    return False
