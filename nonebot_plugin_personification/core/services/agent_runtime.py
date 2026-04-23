from pathlib import Path
from typing import Any, Awaitable, Callable

from ...agent.inner_state import (
    get_personification_data_dir,
    load_inner_state,
    update_inner_state_after_chat,
)
from ...agent.tool_registry import AgentTool, ToolRegistry
from ...skill_runtime.loader import load_builtin_skillpacks_sync
from ...skill_runtime.runtime_api import SkillRuntime
from ..ai_routes import build_routed_tool_caller
from ..file_sender import build_file_sender
from ..session_store import init_session_store
from ..tasks_service import make_cancel_task_tool, make_create_task_tool
from ..web_grounding import do_web_search as do_web_search_core
from ...utils import init_utils_config
from ..sticker_library import resolve_sticker_dir


def build_agent_tool_registry(
    *,
    plugin_config: Any,
    logger: Any,
    get_now: Callable[[], Any],
    tool_caller: Any = None,
    persona_store: Any = None,
    vision_caller: Any = None,
    scheduler: Any = None,
    data_dir: Any = None,
    get_bots: Callable[[], dict[str, Any]] | None = None,
    knowledge_store: Any = None,
    memory_store: Any = None,
    profile_service: Any = None,
    memory_curator: Any = None,
    background_intelligence: Any = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    skills_root_raw = getattr(plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None
    use_skillpacks = bool(getattr(plugin_config, "personification_use_skillpacks", False))

    if use_skillpacks:
        file_sender = build_file_sender(get_bots=get_bots or (lambda: {}), logger=logger)
        runtime = SkillRuntime(
            plugin_config=plugin_config,
            logger=logger,
            get_now=get_now,
            scheduler=scheduler,
            data_dir=data_dir,
            persona_store=persona_store,
            vision_caller=vision_caller,
            file_sender=file_sender,
            get_bots=get_bots,
            tool_caller=tool_caller,
            knowledge_store=knowledge_store,
            memory_store=memory_store,
            profile_service=profile_service,
            memory_curator=memory_curator,
            background_intelligence=background_intelligence,
        )
        try:
            load_builtin_skillpacks_sync(runtime=runtime, registry=registry)
        except Exception as e:
            logger.warning(f"[skillpack] sync load failed: {e}")

    if not use_skillpacks:
        from ...skills.skillpacks.acg_resolver.scripts.main import build_tools as build_acg_tools
        from ...skills.skillpacks.datetime_tool.scripts.impl import build_datetime_tool
        from ...skills.skillpacks.memory_palace.scripts.main import build_tools as build_memory_tools
        from ...skills.skillpacks.plugin_knowledge.scripts.impl import (
            build_plugin_knowledge_tools,
        )
        from ...skills.skillpacks.resource_collector.scripts.main import (
            build_tools as build_resource_tools,
        )
        from ...skills.skillpacks.vision_analyze.scripts.main import (
            build_tools as build_vision_tools,
        )
        from ...skills.skillpacks.weather.scripts.impl import build_weather_tool
        from ...skills.skillpacks.web_search.scripts.impl import build_web_search_tool
        from ...skills.skillpacks.wiki_search.scripts.main import build_tools as build_wiki_tools

        legacy_runtime = SkillRuntime(
            plugin_config=plugin_config,
            logger=logger,
            get_now=get_now,
            scheduler=scheduler,
            data_dir=data_dir,
            persona_store=persona_store,
            vision_caller=vision_caller,
            tool_caller=tool_caller,
            get_bots=get_bots,
            knowledge_store=knowledge_store,
            memory_store=memory_store,
            profile_service=profile_service,
            memory_curator=memory_curator,
            background_intelligence=background_intelligence,
        )

        registry.register(
            build_web_search_tool(
                skills_root=skills_root,
                get_now=get_now,
                logger=logger,
                plugin_config=plugin_config,
            )
        )
        registry.register(build_weather_tool(skills_root, logger))
        registry.register(
            build_datetime_tool(
                timezone_name=getattr(plugin_config, "personification_timezone", "Asia/Shanghai"),
            )
        )
        for tool in build_plugin_knowledge_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_wiki_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_resource_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_vision_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_acg_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_memory_tools(legacy_runtime):
            registry.register(tool)

    if not use_skillpacks:
        from ...skills.skillpacks.sticker_tool.scripts.impl import (
            build_analyze_image_tool,
            build_select_sticker_tool,
            build_understand_sticker_tool,
        )

        sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None))
        if sticker_dir.exists() and sticker_dir.is_dir():
            registry.register(
                build_select_sticker_tool(
                    sticker_dir,
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
                legacy_runtime,
                _image_web_search,
            )
        )
        registry.register(build_understand_sticker_tool(legacy_runtime))

    if persona_store is not None:
        registry.register(
            _build_get_persona_tool(
                persona_store,
                max_chars=max(
                    0,
                    int(getattr(plugin_config, "personification_persona_prompt_max_chars", 120) or 120),
                ),
            )
        )

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

        registry.register(
            AgentTool(
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
            )
        )
        registry.register(
            AgentTool(
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
            )
        )

    if (not use_skillpacks) and getattr(plugin_config, "personification_60s_enabled", True):
        from ...skills.skillpacks.news.scripts.impl import (
            build_baike_tool,
            build_daily_news_tool,
            build_epic_games_tool,
            build_exchange_rate_tool,
            build_gold_price_tool,
            build_history_today_tool,
            build_joke_tool,
            build_trending_tool,
        )

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


def _build_get_persona_tool(persona_store: Any, max_chars: int = 120) -> AgentTool:
    async def _handler(user_id: str) -> str:
        text = persona_store.get_persona_snippet(str(user_id), max_chars=max_chars)
        if not text:
            return f"用户 {user_id} 暂无画像数据。"
        return text

    return AgentTool(
        name="get_user_persona",
        description=(
            "查询指定用户的特征摘要。"
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


def _build_agent_tool_caller(plugin_config: Any, logger: Any) -> Any:
    return build_routed_tool_caller(
        plugin_config=plugin_config,
        logger=logger,
    )


def _build_lite_tool_caller(plugin_config: Any, logger: Any, default_caller: Any = None) -> Any:
    lite_model = str(getattr(plugin_config, "personification_lite_model", "") or "").strip()
    if not lite_model:
        return default_caller
    try:
        return build_routed_tool_caller(
            plugin_config=plugin_config,
            logger=logger,
            model_override=lite_model,
        )
    except Exception:
        return default_caller


def _build_compress_tool_caller(plugin_config: Any, logger: Any) -> Any:
    return build_routed_tool_caller(
        plugin_config=plugin_config,
        logger=logger,
    )


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
        state_tool_caller = build_routed_tool_caller(
            plugin_config=plugin_config,
            logger=logger,
            thinking_mode_override=state_thinking_mode,
        )
    except Exception:
        state_tool_caller = tool_caller

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
    knowledge_store: Any = None,
    memory_store: Any = None,
    profile_service: Any = None,
    memory_curator: Any = None,
    background_intelligence: Any = None,
 ) -> tuple[Any, Any, Any, Any]:
    compress_tool_caller = _build_compress_tool_caller(plugin_config, logger)
    init_session_store(plugin_config, compress_tool_caller)
    init_utils_config(plugin_config)

    if not getattr(plugin_config, "personification_agent_enabled", True):
        return None, None, None, None

    tool_caller = _build_agent_tool_caller(plugin_config, logger)
    lite_tool_caller = _build_lite_tool_caller(plugin_config, logger, tool_caller)
    registry = build_agent_tool_registry(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_now,
        tool_caller=tool_caller,
        persona_store=persona_store,
        vision_caller=vision_caller,
        scheduler=scheduler,
        data_dir=data_dir,
        get_bots=get_bots,
        knowledge_store=knowledge_store,
        memory_store=memory_store,
        profile_service=profile_service,
        memory_curator=memory_curator,
        background_intelligence=background_intelligence,
    )
    inner_state_updater = build_inner_state_updater(
        plugin_config=plugin_config,
        tool_caller=tool_caller,
        logger=logger,
        persona_store=persona_store,
    )
    return registry, inner_state_updater, tool_caller, lite_tool_caller
