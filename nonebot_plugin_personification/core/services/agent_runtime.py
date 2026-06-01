from pathlib import Path
from typing import Any, Awaitable, Callable

from ...agent.inner_state import (
    get_personification_data_dir,
    load_inner_state,
    update_inner_state_after_chat,
)
from ...agent.tool_registry import AgentTool, ToolRegistry
from ...agent.runtime.tool_catalog import apply_tool_metadata_defaults
from ...skill_runtime.loader import load_builtin_skillpacks_sync
from ...skill_runtime.runtime_api import SkillRuntime
from ..ai_routes import build_routed_tool_caller
from ..file_sender import build_file_sender
from ..llm_context import current_llm_context
from ..model_router import (
    MODEL_ROLE_AGENT,
    MODEL_ROLE_INTENT,
    get_model_override_for_role,
)
from ..session_store import init_session_store
from ..tasks_service import make_cancel_task_tool, make_create_task_tool
from ..web_fetch import WebFetchError, fetch_web_page
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
        from ...skills.skillpacks.parallel_research.scripts.main import (
            build_tools as build_parallel_research_tools,
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
        for tool in build_parallel_research_tools(legacy_runtime):
            registry.register(tool)
        for tool in build_memory_tools(legacy_runtime):
            registry.register(tool)
        try:
            from ...skills.skillpacks.image_gen.scripts.main import build_tools as build_image_gen_tools

            for tool in build_image_gen_tools(legacy_runtime):
                registry.register(tool)
        except Exception as exc:
            logger.warning(f"[skillpack] image_gen load failed: {exc}")

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

    if memory_store is not None and bool(getattr(plugin_config, "personification_memory_enabled", True)):
        registry.register(
            _build_recall_user_memory_tool(memory_store, plugin_config, logger)
        )

    if bool(getattr(plugin_config, "personification_tool_web_fetch_enabled", True)):
        registry.register(_build_web_fetch_tool(plugin_config, logger))

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
            build_ai_news_tool,
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
        registry.register(build_ai_news_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_trending_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_joke_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_history_today_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_epic_games_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_gold_price_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_baike_tool(_60s_base, logger, _60s_local_base))
        registry.register(build_exchange_rate_tool(_60s_base, logger, _60s_local_base))
    apply_tool_metadata_defaults(registry)
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


def _build_recall_user_memory_tool(memory_store: Any, plugin_config: Any, logger: Any) -> AgentTool:
    import json as _json

    async def _handler(query: str, days: int = 30, limit: int = 8) -> str:
        ctx = current_llm_context()
        user_id = str(ctx.get("user_id", "") or "").strip()
        if not user_id:
            return _json.dumps({"query": query, "memories": [], "note": "无法确定当前用户，跳过记忆召回"}, ensure_ascii=False)
        try:
            memories = memory_store.recall_memories(
                query=query,
                scope="auto",
                user_id=user_id,
                limit=max(1, min(int(limit or 8), 20)),
                mode="auto",
                context_type="private",
            )
        except Exception as exc:
            logger.debug(f"[recall_user_memory] recall failed: {exc}")
            memories = []
        from ..search_ranker import build_time_hint
        items = []
        for m in memories[:max(1, int(limit or 8))]:
            summary = str(m.get("summary", "") or "").strip()
            if not summary:
                continue
            time_hint = str(m.get("time_hint", "") or build_time_hint(float(m.get("time_created", 0) or 0))).strip()
            memory_type = str(m.get("memory_type", "") or "").strip()
            items.append(f"[{time_hint}] {summary}（{memory_type}）")
        if not items:
            return _json.dumps({"query": query, "memories": []}, ensure_ascii=False)
        return _json.dumps({"query": query, "memories": items}, ensure_ascii=False)

    return AgentTool(
        name="recall_user_memory",
        description=(
            "当用户询问 '你记得我...吗/上次/之前/前几天/复述' 等涉及"
            "跨对话历史时调用。按 user_id 召回该用户在过去 N 天内的 episodic"
            "记忆与所有 semantic 记忆，用于回答记忆类问题。"
            "不要用 search_plugin_knowledge 处理用户记忆问题。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询，例如用户原话"},
                "days": {"type": "integer", "description": "回溯天数，默认30", "default": 30},
                "limit": {"type": "integer", "description": "返回记忆条数上限，默认8", "default": 8},
            },
            "required": ["query"],
        },
        handler=_handler,
        local=True,
        enabled=lambda: bool(getattr(plugin_config, "personification_memory_enabled", True)),
    )


def _build_web_fetch_tool(plugin_config: Any, logger: Any) -> AgentTool:
    import json as _json

    async def _handler(url: str, max_chars: int = 3000) -> str:
        blocked = list(
            getattr(plugin_config, "personification_tool_web_fetch_blocked_domains", []) or []
        )
        timeout = float(
            getattr(plugin_config, "personification_tool_web_fetch_timeout", 60.0) or 60.0
        )
        proxy = str(getattr(plugin_config, "personification_web_proxy", "") or "").strip()
        try:
            result = await fetch_web_page(
                url,
                timeout=timeout,
                max_chars=int(max_chars or 3000),
                blocked_domains=blocked or None,
                proxy=proxy or None,
            )
        except WebFetchError as exc:
            logger.debug(f"[web_fetch] {exc}")
            return _json.dumps({"error": str(exc), "url": str(url or "")}, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"[web_fetch] unexpected error: {exc}")
            return _json.dumps(
                {"error": f"未知错误：{exc}", "url": str(url or "")}, ensure_ascii=False
            )
        return _json.dumps(result, ensure_ascii=False)

    return AgentTool(
        name="web_fetch",
        description=(
            "抓取指定 URL 的网页正文。"
            "当用户给出具体链接（含 http/https 的 URL）希望你阅读、总结、解释"
            "或回答页面里的具体内容时调用。"
            "与 web_search 的区别：web_search 是按关键词搜索摘要，"
            "web_fetch 是直接打开用户给的 URL 把正文抓回来。"
            "默认抓取 3000 字（max_chars 可调，上限 8000），整体超时 60 秒；"
            "拒绝内网/本地地址，仅支持 http/https。"
            "返回 JSON 含 url/title/text/status_code，失败时含 error 字段。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整 URL，必须以 http:// 或 https:// 开头",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "返回正文的最大字符数，默认 3000，上限 8000",
                    "default": 3000,
                },
            },
            "required": ["url"],
        },
        handler=_handler,
        local=True,
        enabled=lambda: bool(
            getattr(plugin_config, "personification_tool_web_fetch_enabled", True)
        ),
    )


def _build_agent_tool_caller(plugin_config: Any, logger: Any) -> Any:
    return build_routed_tool_caller(
        plugin_config=plugin_config,
        logger=logger,
        model_override=get_model_override_for_role(plugin_config, MODEL_ROLE_AGENT),
    )


def _build_lite_tool_caller(plugin_config: Any, logger: Any, default_caller: Any = None) -> Any:
    # P10：严格主模型模式下，所有 lite 路径都共用主模型，避免偶尔降级到弱模型
    # 导致 bot "突然变傻"。默认开启；要恢复旧行为把此开关关掉即可。
    if bool(getattr(plugin_config, "personification_strict_main_model", True)):
        if logger is not None:
            try:
                configured_lite = str(getattr(plugin_config, "personification_lite_model", "") or "").strip()
                if configured_lite:
                    logger.warning(
                        f"[strict_main_model] 已启用，配置的 personification_lite_model="
                        f"{configured_lite!r} 将被忽略；要恢复 lite 路径请把 "
                        f"personification_strict_main_model 关闭。"
                    )
                else:
                    logger.info("[strict_main_model] 启用：lite_tool_caller 复用主模型 caller")
            except Exception:
                pass
        return default_caller
    lite_model = (
        get_model_override_for_role(plugin_config, MODEL_ROLE_INTENT)
        or str(getattr(plugin_config, "personification_lite_model", "") or "").strip()
    )
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
