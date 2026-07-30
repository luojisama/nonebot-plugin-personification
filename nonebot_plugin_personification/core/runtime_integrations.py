from __future__ import annotations

from typing import Any, Callable

from .favorability import FavorabilityService, build_external_sign_in_adapter


def build_sign_in_fallbacks(plugin_config: Any = None, logger: Any = None) -> tuple[bool, Any, Any, Any, Any, bool, Any]:
    """Build the favorability adapter used by legacy runtime fields.

    The public runtime still calls these values sign_in_* for compatibility,
    but the implementation is now plugin-owned. The old sign-in plugin is only
    used as a migration/mirroring source when it exists.
    """

    external = build_external_sign_in_adapter()
    service = FavorabilityService(
        plugin_config=plugin_config,
        external=external,
        logger=logger,
    )
    return (
        service.enabled,
        service.get_user_data,
        service.update_user_data,
        service.load_data,
        service.get_level_name,
        external.available,
        service,
    )


def get_scheduler() -> Any:
    try:
        from nonebot_plugin_apscheduler import scheduler

        return scheduler
    except Exception:
        return None


def extract_default_bot_nickname(load_prompt: Callable[[str | None], Any], logger: Any) -> str:
    try:
        prompt_data = load_prompt(None)
        if isinstance(prompt_data, dict):
            name = str(prompt_data.get("name", "")).strip()
            if name:
                return name
    except Exception as exc:
        logger.debug(f"拟人插件：提取默认昵称失败，使用回退昵称。{exc}")
    return ""


__all__ = [
    "build_sign_in_fallbacks",
    "extract_default_bot_nickname",
    "get_scheduler",
]
