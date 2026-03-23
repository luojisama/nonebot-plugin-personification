import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..agent.inner_state import (
    get_personification_data_dir,
    load_inner_state,
    update_inner_state_after_chat,
)
from ..agent.tool_registry import AgentTool, ToolRegistry
from .prompt_loader import load_prompt as load_prompt_core
from .provider_router import (
    call_ai_api as call_ai_api_core,
    get_configured_api_providers as get_configured_api_providers_core,
)
from ..skills.datetime_tool import build_datetime_tool
from ..skills.news import (
    build_baike_tool,
    build_daily_news_tool,
    build_epic_games_tool,
    build_exchange_rate_tool,
    build_gold_price_tool,
    build_history_today_tool,
    build_joke_tool,
    build_trending_tool,
)
from ..skills.sticker_tool import build_analyze_image_tool, build_select_sticker_tool
from ..skills.tool_caller import build_tool_caller
from ..skills.user_tasks import make_cancel_task_tool, make_create_task_tool
from ..skills.weather import build_weather_tool
from ..skills.web_search import build_web_search_tool
from .runtime_config import (
    load_plugin_runtime_config as load_plugin_runtime_config_core,
    save_plugin_runtime_config as save_plugin_runtime_config_core,
)
from .runtime_state import is_msg_processed as is_msg_processed_core
from .session_store import init_session_store
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
) -> Callable[[List[Dict[str, Any]], Optional[List[Dict[str, Any]]], Optional[int], float], Awaitable[Optional[str]]]:
    async def _call_ai_api(
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.7,
    ) -> Optional[str]:
        response = await call_ai_api_core(
            messages,
            plugin_config=plugin_config,
            logger=logger,
            tools=tools,
            use_builtin_search=False,
        )
        _ = max_tokens, temperature
        return response.content or None

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
        try:
            from plugin.sign_in.utils import get_user_data  # type: ignore
        except ImportError:
            from ...sign_in.utils import get_user_data  # type: ignore
    except Exception:
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


def build_agent_tool_registry(
    *,
    plugin_config: Any,
    logger: Any,
    get_now: Callable[[], Any],
    persona_store: Any = None,
    vision_caller: Any = None,
    scheduler: Any = None,
    data_dir: Any = None,
    get_bots: Callable[[], dict[str, Any]] | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    skills_root_raw = getattr(plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None

    registry.register(
        build_web_search_tool(
            skills_root=skills_root,
            get_now=get_now,
            logger=logger,
        )
    )
    registry.register(build_weather_tool(skills_root, logger))
    registry.register(
        build_datetime_tool(
            timezone_name=getattr(plugin_config, "personification_timezone", "Asia/Shanghai"),
        )
    )

    sticker_path = getattr(plugin_config, "personification_sticker_path", None)
    if sticker_path:
        registry.register(
            build_select_sticker_tool(
                Path(sticker_path),
                plugin_config,
                skills_root=skills_root,
            )
        )
    async def _image_web_search(query: str) -> str:
        return await do_web_search_core(
            query,
            get_now=get_now,
            logger=logger,
        )

    registry.register(
        build_analyze_image_tool(
            vision_caller,
            _image_web_search,
        )
    )
    if persona_store is not None:
        registry.register(_build_get_persona_tool(persona_store))

    # user_tasks 工具
    if scheduler is not None and data_dir is not None:
        _bot_caller: Callable[[dict], Any] | None = None
        if get_bots is not None:
            async def _bot_caller(task: dict) -> None:
                if not isinstance(task, dict):
                    return
                params = task.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                user_id = task.get("user_id") or params.get("user_id")
                message = params.get("message") or task.get("message", "")
                if not user_id or not message:
                    return
                for bot in get_bots().values():
                    try:
                        await bot.send_private_msg(
                            user_id=int(user_id),
                            message=str(message),
                        )
                        return
                    except Exception:
                        continue

        registry.register(AgentTool(
            name="create_user_task",
            description=(
                "当用户要求定期执行某件事时调用（如'每天8点发天气'）。"
                "将任务持久化，重启不丢失。cron 为标准五段式，如 '0 8 * * *'。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号"},
                    "description": {"type": "string", "description": "任务自然语言描述"},
                    "cron": {"type": "string", "description": "cron 表达式，5段式"},
                    "action": {"type": "string", "description": "执行动作类型"},
                    "params": {"type": "object", "description": "动作参数"},
                },
                "required": ["user_id", "description", "cron", "action"],
            },
            handler=make_create_task_tool(scheduler, data_dir, _bot_caller),
        ))
        registry.register(AgentTool(
            name="cancel_user_task",
            description="取消用户之前设置的定时任务。",
            parameters={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "用户QQ号"},
                    "task_id": {"type": "string", "description": "任务ID，如 task_001"},
                },
                "required": ["user_id", "task_id"],
            },
            handler=make_cancel_task_tool(scheduler, data_dir),
        ))

    # 60s API 工具
    if getattr(plugin_config, "personification_60s_enabled", True):
        _60s_base = str(
            getattr(plugin_config, "personification_60s_api_base", "https://60s.viki.moe") or ""
        ).strip().rstrip("/") or "https://60s.viki.moe"
        _60s_local_base = str(
            getattr(plugin_config, "personification_60s_local_api_base", "http://127.0.0.1:4399") or ""
        ).strip().rstrip("/") or "http://127.0.0.1:4399"
        registry.register(build_daily_news_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_trending_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_joke_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_history_today_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_epic_games_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_gold_price_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_baike_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_exchange_rate_tool(_60s_base, logger, _60s_local_base))
    return registry


def _build_get_persona_tool(persona_store: Any) -> AgentTool:
    async def _handler(user_id: str) -> str:
        text = persona_store.get_persona_text(str(user_id))
        if not text:
            return f"用户 {user_id} 暂无画像数据。"
        return text

    return AgentTool(
        name="get_user_persona",
        description=(
            "查询指定用户的画像分析，包括职业推测、年龄推测、性别推测和人物描述。"
            "当你想了解某人的特征、判断话题是否适合对方时调用。"
            "user_id 是对方的 QQ 号。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "要查询的用户 QQ 号",
                }
            },
            "required": ["user_id"],
        },
        handler=_handler,
        local=True,
    )


def _build_tool_caller_override(config: Any, thinking_mode: str) -> Any:
    """构建覆盖了 thinking_mode 的独立 ToolCaller，供 inner_state 更新使用。"""

    class _ConfigProxy:
        """轻量代理，只覆盖 thinking_mode 字段，其余全部透传原始 config。"""

        def __init__(self, original: Any, mode: str) -> None:
            self._original = original
            self._mode = mode

        def __getattr__(self, name: str) -> Any:
            if name == "personification_thinking_mode":
                return self._mode
            return getattr(self._original, name)

    return build_tool_caller(_ConfigProxy(config, thinking_mode))


def _build_agent_tool_caller(plugin_config: Any, logger: Any) -> Any:
    providers = get_configured_api_providers_core(plugin_config, logger)
    if not providers:
        return build_tool_caller(plugin_config)

    primary = providers[0]

    class _ConfigProxy:
        def __init__(self, original: Any, provider: Dict[str, Any]) -> None:
            self._original = original
            self._provider = provider

        def __getattr__(self, name: str) -> Any:
            if name == "personification_api_type":
                return self._provider.get("api_type", "openai")
            if name == "personification_api_url":
                return self._provider.get("api_url", "")
            if name == "personification_api_key":
                return self._provider.get("api_key", "")
            if name == "personification_model":
                return self._provider.get("model", "")
            if name == "personification_codex_auth_path":
                return self._provider.get("auth_path", "")
            return getattr(self._original, name)

    logger.info(
        "personification: agent tool caller provider="
        f"{primary.get('name', 'unknown')} type={primary.get('api_type', 'openai')}"
    )
    return build_tool_caller(_ConfigProxy(plugin_config, primary))


def _build_compress_tool_caller(plugin_config: Any) -> Any:
    """构建压缩专用 ToolCaller；留空时回退到主对话模型配置。"""

    compress_api_type = str(
        getattr(plugin_config, "personification_compress_api_type", "") or ""
    ).strip()
    compress_api_url = str(
        getattr(plugin_config, "personification_compress_api_url", "") or ""
    ).strip()
    compress_api_key = str(
        getattr(plugin_config, "personification_compress_api_key", "") or ""
    ).strip()
    compress_model = str(
        getattr(plugin_config, "personification_compress_model", "") or ""
    ).strip()
    use_primary = not any([
        compress_api_type,
        compress_api_url,
        compress_api_key,
        compress_model,
    ])

    class _CompressConfigProxy:
        def __init__(self, original: Any) -> None:
            self._original = original

        def __getattr__(self, name: str) -> Any:
            if name == "personification_api_type":
                if use_primary:
                    return getattr(self._original, "personification_api_type", "openai")
                return compress_api_type
            if name == "personification_api_url":
                if use_primary:
                    return getattr(self._original, "personification_api_url", "")
                return compress_api_url
            if name == "personification_api_key":
                if use_primary:
                    return getattr(self._original, "personification_api_key", "")
                return compress_api_key
            if name == "personification_model":
                if use_primary:
                    return getattr(self._original, "personification_model", "gpt-4o-mini")
                return compress_model
            if name == "personification_codex_auth_path":
                return getattr(self._original, "personification_codex_auth_path", "")
            return getattr(self._original, name)

    return build_tool_caller(_CompressConfigProxy(plugin_config))


def build_inner_state_updater(
    *,
    plugin_config: Any,
    tool_caller: Any,
    logger: Any,
    persona_store: Any = None,
) -> Callable[[str, str], Awaitable[None]]:
    data_dir = get_personification_data_dir(plugin_config)
    state_thinking_mode = str(
        getattr(plugin_config, "personification_state_thinking_mode", "adaptive") or "adaptive"
    ).strip()
    try:
        state_tool_caller = _build_tool_caller_override(plugin_config, state_thinking_mode)
    except Exception:
        state_tool_caller = tool_caller  # 降级：复用主对话 caller

    async def _inner_state_updater(text: str, user_id: str = "") -> None:
        current_state = await load_inner_state(data_dir)
        persona_snippet = ""
        if persona_store and user_id:
            persona_snippet = persona_store.get_persona_snippet(
                user_id,
                max_chars=max(
                    1,
                    int(getattr(plugin_config, "personification_persona_snippet_max_chars", 150)),
                ),
            )
        await update_inner_state_after_chat(
            data_dir,
            state_tool_caller,
            text,
            current_state,
            state_thinking_mode,
            logger,
            persona_snippet=persona_snippet,
        )

    return _inner_state_updater


def build_agent_runtime_deps(
    *,
    plugin_config: Any,
    logger: Any,
    get_now: Callable[[], Any],
    persona_store: Any = None,
    vision_caller: Any = None,
    scheduler: Any = None,
    data_dir: Any = None,
    get_bots: Callable[[], dict[str, Any]] | None = None,
) -> tuple[Any, Any, Any]:
    compress_tool_caller = _build_compress_tool_caller(plugin_config)
    init_session_store(plugin_config, compress_tool_caller)

    if not getattr(plugin_config, "personification_agent_enabled", True):
        return None, None, None

    tool_caller = _build_agent_tool_caller(plugin_config, logger)
    registry = build_agent_tool_registry(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_now,
        persona_store=persona_store,
        vision_caller=vision_caller,
        scheduler=scheduler,
        data_dir=data_dir,
        get_bots=get_bots,
    )
    inner_state_updater = build_inner_state_updater(
        plugin_config=plugin_config,
        tool_caller=tool_caller,
        logger=logger,
        persona_store=persona_store,
    )
    return registry, inner_state_updater, tool_caller
