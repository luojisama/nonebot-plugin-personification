import time
from typing import Any, Callable, Optional

import httpx

_shared_http_client: Optional[httpx.AsyncClient] = None


def schedule_disabled_override_prompt() -> str:
    """作息关闭时用于覆盖模板内残留约束的提示片段。"""
    return (
        "## 作息开关状态（最高优先级）\n"
        "- 当前配置：作息模拟已关闭。\n"
        "- 忽略所有与“作息/上课/睡觉/深夜限制”相关的规则、示例和状态要求（即使模板中出现，也不生效）。\n"
        "- 你可以在任意时间正常对话，不要因为时间点而拒绝回复或结束对话。"
    )


def get_shared_http_client(*, max_connections: int = 20) -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None or _shared_http_client.is_closed:
        _shared_http_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=max_connections),
        )
    return _shared_http_client


async def close_shared_http_client(*, logger: Any = None) -> None:
    global _shared_http_client
    if _shared_http_client is None:
        return
    try:
        await _shared_http_client.aclose()
    except Exception as e:
        if logger is not None:
            logger.warning(f"拟人插件：关闭共享 HTTP 客户端失败: {e}")
    finally:
        _shared_http_client = None


def is_msg_processed(
    message_id: int,
    *,
    get_driver: Callable[[], Any],
    logger: Any,
    module_instance_id: int,
    now_fn: Callable[[], float] = time.time,
    max_cache: int = 100,
    ttl_seconds: int = 60,
) -> bool:
    """检查消息是否已处理（跨实例共享缓存）。"""
    driver = get_driver()
    if not hasattr(driver, "_personification_msg_cache"):
        driver._personification_msg_cache = {}

    cache = driver._personification_msg_cache
    now = now_fn()

    if len(cache) > max_cache:
        expired = [mid for mid, ts in cache.items() if now - ts > ttl_seconds]
        for mid in expired:
            del cache[mid]

    if message_id in cache:
        logger.debug(f"拟人插件：[Inst {module_instance_id}] 拦截重复消息 ID: {message_id}")
        return True

    cache[message_id] = now
    logger.debug(f"拟人插件：[Inst {module_instance_id}] 开始处理新消息 ID: {message_id}")
    return False
