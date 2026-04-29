import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..prompt_loader import load_prompt as load_prompt_core
from ..provider_router import call_ai_api as call_ai_api_core
from ..provider_router import (
    get_configured_api_providers as get_configured_api_providers_core,
)
from ..model_router import get_model_override_for_role
from ..runtime_state import is_msg_processed as is_msg_processed_core
from ..web_grounding import (
    build_grounding_context as build_grounding_context_core,
    do_web_search as do_web_search_core,
    should_avoid_interrupting as should_avoid_interrupting_core,
)


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
    plugin_config: Any,
    get_now: Callable[[], Any],
    logger: Any,
) -> Callable[[str], Awaitable[str]]:
    from ...skills.skillpacks.wiki_search.scripts.main import (
        resolve_wiki_runtime_config,
    )

    wiki_enabled, fandom_enabled, fandom_wikis = resolve_wiki_runtime_config(plugin_config)

    async def _build_grounding_context(user_text: str, context_hint: str = "") -> str:
        return await build_grounding_context_core(
            user_text,
            web_search_enabled=bool(
                getattr(
                    plugin_config,
                    "personification_tool_web_search_enabled",
                    getattr(plugin_config, "personification_web_search", False),
                )
            ),
            context_hint=context_hint,
            get_now=get_now,
            logger=logger,
            wiki_enabled=wiki_enabled,
            wiki_fandom_enabled=fandom_enabled,
            fandom_wikis=fandom_wikis,
        )

    return _build_grounding_context


def build_interrupt_guard(
    *,
    get_recent_group_msgs: Callable[[str, int], list[dict]],
    hot_chat_min_pass_rate: float = 0.2,
) -> Callable[[str, bool], bool]:
    def _should_avoid_interrupting(group_id: str, is_random_chat: bool) -> bool:
        return should_avoid_interrupting_core(
            group_id,
            is_random_chat=is_random_chat,
            get_recent_group_msgs=get_recent_group_msgs,
            now_ts=int(time.time()),
            hot_chat_min_pass_rate=hot_chat_min_pass_rate,
        )

    return _should_avoid_interrupting


def build_web_search_executor(
    *,
    get_now: Callable[[], Any],
    logger: Any,
) -> Callable[[str], Awaitable[str]]:
    async def _do_web_search(query: str, context_hint: str = "") -> str:
        return await do_web_search_core(
            query,
            context_hint=context_hint,
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
    model_override_field_name: str = "",
    model_role: str = "",
) -> Callable[
    [List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Optional[int], float],
    Awaitable[Optional[str]],
]:
    async def _call_ai_api(
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> Optional[str]:
        model_override = ""
        if model_role:
            model_override = get_model_override_for_role(plugin_config, model_role)
        if not model_override and model_override_field_name:
            model_override = str(getattr(plugin_config, model_override_field_name, "") or "").strip()
        response = await call_ai_api_core(
            messages,
            plugin_config=plugin_config,
            logger=logger,
            tools=tools,
            use_builtin_search=bool(
                getattr(
                    plugin_config,
                    "personification_model_builtin_search_enabled",
                    getattr(plugin_config, "personification_builtin_search", True),
                )
            ),
            model_override=model_override,
        )
        _ = max_tokens, temperature
        if response.vision_unavailable:
            raise RuntimeError("image input not supported by configured providers")
        return response.content or None

    return _call_ai_api
