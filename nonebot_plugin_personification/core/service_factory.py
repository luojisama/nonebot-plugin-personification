import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .prompt_loader import load_prompt as load_prompt_core
from .provider_router import (
    call_ai_api as call_ai_api_core,
    get_configured_api_providers as get_configured_api_providers_core,
)
from .runtime_config import (
    load_plugin_runtime_config as load_plugin_runtime_config_core,
    save_plugin_runtime_config as save_plugin_runtime_config_core,
)
from .runtime_state import is_msg_processed as is_msg_processed_core
from .web_grounding import (
    build_grounding_context as build_grounding_context_core,
    do_web_search as do_web_search_core,
    should_avoid_interrupting as should_avoid_interrupting_core,
)
from .sticker_cache import get_sticker_files as get_sticker_files_core


def build_load_prompt(
    *,
    plugin_config: Any,
    get_group_config: Callable[[str], Dict[str, Any]],
    logger: Any,
) -> Callable[[Optional[str]], Any]:
    def _load_prompt(group_id: Optional[str] = None) -> Any:
        return load_prompt_core(
            plugin_config=plugin_config,
            get_group_config=get_group_config,
            logger=logger,
            group_id=group_id,
        )

    return _load_prompt


def build_msg_processed_checker(
    *,
    get_driver: Callable[[], Any],
    logger: Any,
    module_instance_id: int,
) -> Callable[[int], bool]:
    def _is_msg_processed(message_id: int) -> bool:
        return is_msg_processed_core(
            message_id,
            get_driver=get_driver,
            logger=logger,
            module_instance_id=module_instance_id,
            now_fn=time.time,
        )

    return _is_msg_processed


def build_grounding_context_builder(
    *,
    web_search_enabled: bool,
    get_now: Callable[[], Any],
    logger: Any,
) -> Callable[[str], Awaitable[str]]:
    async def _build_grounding_context(user_text: str) -> str:
        return await build_grounding_context_core(
            user_text,
            web_search_enabled=web_search_enabled,
            get_now=get_now,
            logger=logger,
        )

    return _build_grounding_context


def build_interrupt_guard(
    *,
    get_recent_group_msgs: Callable[[str, int], list[dict]],
) -> Callable[[str, bool], bool]:
    def _should_avoid_interrupting(group_id: str, is_random_chat: bool) -> bool:
        return should_avoid_interrupting_core(
            group_id,
            is_random_chat=is_random_chat,
            get_recent_group_msgs=get_recent_group_msgs,
            now_ts=int(time.time()),
        )

    return _should_avoid_interrupting


def build_web_search_executor(
    *,
    get_now: Callable[[], Any],
    logger: Any,
) -> Callable[[str], Awaitable[str]]:
    async def _do_web_search(query: str) -> str:
        return await do_web_search_core(
            query,
            get_now=get_now,
            logger=logger,
        )

    return _do_web_search


def build_provider_reader(
    *,
    plugin_config: Any,
    logger: Any,
) -> Callable[[], List[Dict[str, Any]]]:
    def _get_configured_api_providers() -> List[Dict[str, Any]]:
        return get_configured_api_providers_core(plugin_config, logger)

    return _get_configured_api_providers


def build_ai_api_caller(
    *,
    plugin_config: Any,
    logger: Any,
    do_web_search: Callable[[str], Awaitable[str]],
) -> Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Optional[int], float], Awaitable[Optional[str]]]:
    async def _call_ai_api(
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> Optional[str]:
        return await call_ai_api_core(
            messages,
            plugin_config=plugin_config,
            logger=logger,
            do_web_search=do_web_search,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    return _call_ai_api


def build_runtime_config_io(
    *,
    plugin_config: Any,
    logger: Any,
) -> Tuple[Callable[[], None], Callable[[], None]]:
    def _save_plugin_runtime_config() -> None:
        save_plugin_runtime_config_core(plugin_config, logger)

    def _load_plugin_runtime_config() -> None:
        load_plugin_runtime_config_core(plugin_config, logger)

    return _save_plugin_runtime_config, _load_plugin_runtime_config


def build_custom_title_getter(
    *,
    logger: Any = None,
) -> Callable[[str], str]:
    try:
        from nonebot_plugin_shiro_signin.utils import get_user_data  # type: ignore
    except ImportError:
        try:
            from plugin.sign_in.utils import get_user_data  # type: ignore
        except ImportError:
            if logger is not None:
                logger.debug("拟人插件：未启用签到插件称号读取，使用空称号回退。")
            return lambda _user_id: ""

    def _get_custom_title(user_id: str) -> str:
        try:
            user_data = get_user_data(user_id)
            custom_title = user_data.get("custom_title")
            if custom_title:
                return str(custom_title)
        except Exception:
            pass
        return ""

    return _get_custom_title


def build_sticker_cache(
    *,
    sticker_path: str | Path | None,
    ttl_seconds: int = 300,
) -> Callable[[], List[Path]]:
    def _get_sticker_files() -> List[Path]:
        return get_sticker_files_core(sticker_path, ttl_seconds=ttl_seconds)

    return _get_sticker_files

