from __future__ import annotations

from typing import Any, Callable


def build_sign_in_fallbacks() -> tuple[bool, Any, Any, Any, Any]:
    try:
        try:
            from plugin.sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from plugin.sign_in.config import get_level_name  # type: ignore
        except ImportError:
            from ...sign_in.utils import get_user_data, load_data, update_user_data  # type: ignore
            from ...sign_in.config import get_level_name  # type: ignore
        return True, get_user_data, update_user_data, load_data, get_level_name
    except ImportError:
        return False, (lambda _uid: {}), (lambda *_a, **_k: None), (lambda: {}), (lambda _v: "普通")


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
