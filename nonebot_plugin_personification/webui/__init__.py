from __future__ import annotations

from typing import Any, Callable

from .app import build_router, set_runtime_context


def install_webui(
    *,
    plugin_config: Any,
    superusers: set[str] | list[str],
    get_bots: Callable[[], dict[str, Any]],
    logger: Any,
    runtime_bundle: Any = None,
) -> bool:
    """在 NoneBot FastAPI driver 上挂载 WebUI 路由。

    返回 True 表示挂载成功；False 表示当前 driver 不是 FastAPI（静默跳过）。
    """
    try:
        from nonebot import get_app
    except Exception:
        if logger is not None:
            logger.warning("personification webui: nonebot.get_app 不可用，跳过 WebUI 挂载")
        return False
    try:
        app = get_app()
    except Exception:
        app = None
    if app is None:
        if logger is not None:
            logger.warning("personification webui: 当前 NoneBot driver 不是 FastAPI，跳过 WebUI 挂载")
        return False
    set_runtime_context(
        plugin_config=plugin_config,
        superusers=set(str(item) for item in (superusers or [])),
        get_bots=get_bots,
        logger=logger,
        runtime_bundle=runtime_bundle,
    )
    router = build_router()
    app.include_router(router)
    if logger is not None:
        logger.info("personification webui: 已挂载路由前缀 /personification")
    return True


__all__ = ["install_webui"]
