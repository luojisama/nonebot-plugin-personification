from __future__ import annotations

from typing import Any, Callable, Iterable


async def send_to_admin(get_bots: Callable[[], dict[str, Any]], qq: str, message: str) -> bool:
    """通过任意可用 bot 给指定 QQ 发私聊。
    返回 True 表示至少一个 bot 成功；返回 False 表示全部失败。
    """
    target = str(qq or "").strip()
    if not target:
        return False
    try:
        bots = get_bots() or {}
    except Exception:
        bots = {}
    for bot in list(bots.values()):
        try:
            await bot.call_api("send_private_msg", user_id=int(target), message=str(message or ""))
            return True
        except Exception:
            continue
    return False


async def startup_notify_admins(
    *,
    get_bots: Callable[[], dict[str, Any]],
    superusers: Iterable[str],
    plugin_admins: Iterable[str],
    message: str,
) -> int:
    """启动后向所有 superuser 与 plugin_admin 推一条通知，返回成功数。"""
    seen: set[str] = set()
    targets: list[str] = []
    for source in (superusers or [], plugin_admins or []):
        for item in source:
            cleaned = str(item or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                targets.append(cleaned)
    success = 0
    for qq in targets:
        if await send_to_admin(get_bots, qq, message):
            success += 1
    return success


__all__ = ["send_to_admin", "startup_notify_admins"]
