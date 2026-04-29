import asyncio
import sys
import types
from pathlib import Path

_module = sys.modules[__name__]
if __name__ == "nonebot_plugin_personification":
    _plugin_root = str(Path(__file__).resolve().parent.parent)
    plugin_namespace = sys.modules.get("plugin")
    if plugin_namespace is None:
        plugin_namespace = types.ModuleType("plugin")
        plugin_namespace.__path__ = [_plugin_root]  # type: ignore[attr-defined]
        sys.modules["plugin"] = plugin_namespace
    else:
        plugin_path = getattr(plugin_namespace, "__path__", None)
        if plugin_path is None:
            plugin_namespace.__path__ = [_plugin_root]  # type: ignore[attr-defined]
        elif _plugin_root not in plugin_path:
            try:
                plugin_path.append(_plugin_root)
            except Exception:
                plugin_namespace.__path__ = list(plugin_path) + [_plugin_root]  # type: ignore[attr-defined]
    setattr(plugin_namespace, "personification", _module)
    sys.modules.setdefault("plugin.personification", _module)

from nonebot import get_bots, get_driver, get_plugin_config, logger, require
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PokeNotifyEvent,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException
from nonebot.permission import SUPERUSER
from nonebot.rule import Rule

try:
    require("nonebot_plugin_apscheduler")
    from nonebot_plugin_apscheduler import scheduler
except Exception as e:
    raise RuntimeError(
        'Cannot load required plugin "nonebot_plugin_apscheduler". '
        'Install it in the active venv first, for example: '
        '"F:\\bot\\shirotest\\.venv\\Scripts\\python.exe -m pip install nonebot-plugin-apscheduler".'
    ) from e

try:
    require("nonebot_plugin_htmlrender")
    from nonebot_plugin_htmlrender import md_to_pic
except Exception as e:
    logger.warning(f'拟人插件：加载 "nonebot_plugin_htmlrender" 失败，渲染能力将降级。{e}')
    md_to_pic = None

from .config import Config
from .core.file_sender import build_file_sender
from .core.config_manager import ConfigManager
from .core.knowledge_builder import (
    maybe_start_plugin_knowledge_builder,
    stop_plugin_knowledge_builder,
)
from .core.plugin_meta import build_plugin_metadata
from .core.plugin_runtime import build_plugin_runtime
from .core.runtime_state import close_shared_http_client
from .core.ai_routes import (
    build_routed_tool_caller,
    format_provider_summary,
    resolve_global_fallback_provider,
)
from .core.visual_capabilities import (
    VISUAL_ROUTE_AGENT,
    VISUAL_ROUTE_REPLY_PLAIN,
    VISUAL_ROUTE_REPLY_YAML,
    VISUAL_ROUTE_VISION,
    build_primary_route_probe_caller,
    probe_tool_caller_vision,
    probe_vision_caller,
    set_visual_capability,
)
from .flows import setup_flows
from .handlers import setup_all_matchers
from .handlers.persona_commands import setup_persona_matchers
from .jobs import setup_jobs
from .schedule import get_current_local_time
from .agent.inner_state import get_personification_data_dir
from .skill_runtime.runtime_api import SkillRuntime
from .skills.skillpacks.sticker_labeler.scripts.impl import StickerLabeler, start_watchdog
from .core.sticker_library import resolve_sticker_dir
from .utils import is_group_whitelisted

plugin_config = get_plugin_config(Config)
ConfigManager(plugin_config=plugin_config, logger=logger).load()
superusers = get_driver().config.superusers
__plugin_meta__ = build_plugin_metadata(Config)

_sticker_labeler_observer = None
_knowledge_build_task: asyncio.Task | None = None
_visual_probe_task: asyncio.Task | None = None
_llm_warmup_task: asyncio.Task | None = None
runtime_bundle = None
flow_handles: dict[str, object] = {}
job_handles: dict[str, object] = {}
matcher_handles: dict[str, object] = {}
persona_matcher_handles: dict[str, object] = {}

personification_rule = None
poke_rule = None
poke_notice_rule = None
check_proactive_messaging = None


def _get_knowledge_build_task() -> asyncio.Task | None:
    return _knowledge_build_task


def _set_knowledge_build_task(task: asyncio.Task | None) -> None:
    global _knowledge_build_task
    _knowledge_build_task = task


def _require_runtime_bundle():
    if runtime_bundle is None:
        raise RuntimeError("personification runtime not initialized yet")
    return runtime_bundle


async def _persona_whitelist_rule(event: MessageEvent) -> bool:
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        return is_group_whitelisted(
            str(event.group_id),
            plugin_config.personification_whitelist,
        )
    return False


async def _probe_visual_routes_on_startup() -> None:
    if runtime_bundle is None:
        return

    probe_caller, primary_api_type, primary_model = build_primary_route_probe_caller(
        plugin_config=plugin_config,
        get_configured_api_providers=runtime_bundle.get_configured_api_providers,
    )
    main_supported = await probe_tool_caller_vision(
        route_name=VISUAL_ROUTE_REPLY_PLAIN,
        caller=probe_caller,
        api_type=primary_api_type,
        model=primary_model,
        logger=logger,
    )
    if main_supported is not None:
        set_visual_capability(
            VISUAL_ROUTE_REPLY_YAML,
            primary_api_type,
            primary_model,
            main_supported,
            source="startup_probe",
            detail="shared_with_plain_route",
        )

    agent_api_type, agent_model = primary_api_type, primary_model
    await probe_tool_caller_vision(
        route_name=VISUAL_ROUTE_AGENT,
        caller=probe_caller,
        api_type=agent_api_type,
        model=agent_model,
        logger=logger,
    )

    fallback_resolution = resolve_global_fallback_provider(plugin_config, logger, warn=True)
    if fallback_resolution is not None:
        await probe_vision_caller(
            route_name=VISUAL_ROUTE_VISION,
            caller=runtime_bundle.reply_processor_deps.runtime.vision_caller,
            api_type=str(fallback_resolution.provider.get("api_type", "") or ""),
            model=str(fallback_resolution.provider.get("model", "") or ""),
            logger=logger,
        )


async def _run_visual_probe_background() -> None:
    try:
        await _probe_visual_routes_on_startup()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(f"[vision] startup background probe failed: {exc}")


def _start_visual_probe_background() -> None:
    global _visual_probe_task
    if _visual_probe_task is not None and not _visual_probe_task.done():
        return
    task = asyncio.create_task(_run_visual_probe_background())
    _visual_probe_task = task

    def _clear_probe_task(done_task: asyncio.Task) -> None:
        global _visual_probe_task
        if _visual_probe_task is done_task:
            _visual_probe_task = None

    task.add_done_callback(_clear_probe_task)


async def _warmup_llm_connection() -> None:
    bundle = _require_runtime_bundle()
    caller = (
        bundle.reply_processor_deps.runtime.agent_tool_caller
        or bundle.reply_processor_deps.runtime.lite_tool_caller
    )
    if caller is None:
        return
    try:
        await asyncio.wait_for(
            caller.chat_with_tools(
                [{"role": "user", "content": "hi"}],
                [],
                False,
            ),
            timeout=30.0,
        )
        logger.info("[warmup] LLM 连接预热完成")
    except Exception as exc:
        logger.debug(f"[warmup] LLM 预热失败（不影响正常使用）: {exc}")


def _start_llm_warmup_background() -> None:
    global _llm_warmup_task
    if _llm_warmup_task is not None and not _llm_warmup_task.done():
        return
    task = asyncio.create_task(_warmup_llm_connection())
    _llm_warmup_task = task

    def _clear_warmup_task(done_task: asyncio.Task) -> None:
        global _llm_warmup_task
        if _llm_warmup_task is done_task:
            _llm_warmup_task = None

    task.add_done_callback(_clear_warmup_task)


@get_driver().on_startup
async def _init_personification_runtime() -> None:
    global runtime_bundle, flow_handles, job_handles, matcher_handles
    global persona_matcher_handles, personification_rule, poke_rule, poke_notice_rule
    global check_proactive_messaging

    if runtime_bundle is not None:
        return

    runtime_bundle = build_plugin_runtime(
        plugin_config=plugin_config,
        superusers=superusers,
        logger=logger,
        get_driver=get_driver,
        get_bots=get_bots,
        superuser_permission=SUPERUSER,
        finished_exception_cls=FinishedException,
        group_message_event_cls=GroupMessageEvent,
        private_message_event_cls=PrivateMessageEvent,
        message_event_cls=MessageEvent,
        poke_event_cls=PokeNotifyEvent,
        message_cls=Message,
        message_segment_cls=MessageSegment,
        md_to_pic=md_to_pic,
    )
    runtime_bundle.get_knowledge_build_task = _get_knowledge_build_task
    runtime_bundle.set_knowledge_build_task = _set_knowledge_build_task
    _start_visual_probe_background()
    _start_llm_warmup_background()

    personification_rule = runtime_bundle.personification_rule
    poke_rule = runtime_bundle.poke_rule
    poke_notice_rule = runtime_bundle.poke_notice_rule

    flow_handles = setup_flows(deps=runtime_bundle.make_flow_setup_deps())
    check_proactive_messaging = flow_handles["check_proactive_messaging"]

    job_handles = setup_jobs(
        scheduler=scheduler,
        deps=runtime_bundle.make_job_setup_deps(
            check_proactive_messaging=check_proactive_messaging,
            check_group_idle_topic=flow_handles.get("check_group_idle_topic"),
        ),
    )

    matcher_handles = setup_all_matchers(
        deps=runtime_bundle.make_matcher_setup_deps(
            generate_ai_diary=job_handles["generate_ai_diary"],
            apply_global_switch=flow_handles["apply_global_switch"],
            apply_tts_global_switch=flow_handles["apply_tts_global_switch"],
            apply_web_search_switch=flow_handles["apply_web_search_switch"],
            apply_proactive_switch=flow_handles["apply_proactive_switch"],
            start_knowledge_builder=__import__(
                "plugin.personification.core.knowledge_builder",
                fromlist=["start_knowledge_builder"],
            ).start_knowledge_builder,
            get_knowledge_build_task=_get_knowledge_build_task,
            set_knowledge_build_task=_set_knowledge_build_task,
        )
    )
    globals().update(matcher_handles)

    if runtime_bundle.persona_store is not None:
        persona_matcher_handles = setup_persona_matchers(
            persona_store=runtime_bundle.persona_store,
            whitelist_rule=Rule(_persona_whitelist_rule),
            superusers=superusers,
            logger=logger,
        )
        globals().update(persona_matcher_handles)


@get_driver().on_startup
async def _init_personification_persona_store() -> None:
    bundle = _require_runtime_bundle()
    if bundle.persona_store is None:
        return
    await bundle.persona_store.load()
    logger.info("[user_persona] 画像数据已加载")


@get_driver().on_startup
async def _restore_user_tasks() -> None:
    from .core.tasks_service import restore_tasks_on_startup

    data_dir = get_personification_data_dir(plugin_config)

    async def _bot_caller(task: dict) -> None:
        params = task.get("params", {}) if isinstance(task, dict) else {}
        user_id = task.get("user_id") if isinstance(task, dict) else None
        if not user_id and isinstance(params, dict):
            user_id = params.get("user_id")
        if not user_id:
            return

        message = ""
        if isinstance(params, dict):
            message = str(params.get("message", "") or "")
        if not message and isinstance(task, dict):
            message = str(task.get("message", "") or "")
        if not message:
            return

        for bot in get_bots().values():
            try:
                friend_ids = set()
                try:
                    friends = await bot.get_friend_list()
                    if isinstance(friends, list):
                        friend_ids = {
                            str(item.get("user_id"))
                            for item in friends
                            if isinstance(item, dict) and item.get("user_id") is not None
                        }
                except Exception as e:
                    logger.warning(f"[user_tasks] 获取好友列表失败: {e}")
                if friend_ids and str(user_id) not in friend_ids:
                    logger.warning(f"[user_tasks] 用户 {user_id} 不在好友列表，跳过发送确认")
                    continue
                await bot.send_private_msg(user_id=int(user_id), message=message)
                task["last_status"] = "sent"
                return
            except Exception as e:
                task["last_status"] = "failed"
                logger.warning(f"[user_tasks] 任务消息发送失败 user={user_id}: {e}")
                continue

    restore_tasks_on_startup(scheduler, data_dir, _bot_caller)
    logger.info("[user_tasks] 持久化定时任务已恢复")


@get_driver().on_startup
async def _load_custom_skills() -> None:
    bundle = _require_runtime_bundle()
    skills_path = getattr(plugin_config, "personification_skills_path", None)

    from .skill_runtime.custom_loader import load_custom_skills

    registry = bundle.tool_registry
    if registry is None:
        return

    tool_caller = bundle.reply_processor_deps.runtime.agent_tool_caller
    if tool_caller is None:
        try:
            tool_caller = build_routed_tool_caller(plugin_config, logger)
        except Exception as e:
            logger.warning(f"[custom_skills] 构建自动分析 tool caller 失败: {e}")
            tool_caller = None

    custom_root = Path(skills_path) if skills_path else None
    if custom_root and not custom_root.exists():
        logger.warning(f"[custom_skills] 自定义 skill 目录不存在：{custom_root}")
        custom_root = None

    runtime = SkillRuntime(
        plugin_config=plugin_config,
        logger=logger,
        get_now=get_current_local_time,
        scheduler=scheduler,
        data_dir=get_personification_data_dir(plugin_config),
        persona_store=bundle.persona_store,
        vision_caller=bundle.reply_processor_deps.runtime.vision_caller,
        file_sender=build_file_sender(get_bots=get_bots, logger=logger),
        get_bots=get_bots,
        get_whitelisted_groups=bundle._get_whitelisted_groups,
        tool_caller=tool_caller,
        knowledge_store=bundle.reply_processor_deps.runtime.knowledge_store,
        memory_store=bundle.memory_store,
        profile_service=bundle.profile_service,
        memory_curator=bundle.memory_curator,
        background_intelligence=bundle.background_intelligence,
    )
    await load_custom_skills(
        custom_root,
        registry,
        logger,
        tool_caller=tool_caller,
        plugin_config=plugin_config,
        runtime=runtime,
    )
    if custom_root is not None or getattr(plugin_config, "personification_skill_sources", None):
        logger.info(
            f"[custom_skills] 已加载内置 skill、本地目录与远程 sources。"
            f" local_root={custom_root}"
        )
    else:
        logger.info("[custom_skills] 已加载内置 skill")


@get_driver().on_startup
async def _init_personification_sticker_labeler() -> None:
    global _sticker_labeler_observer

    sticker_dir = resolve_sticker_dir(getattr(plugin_config, "personification_sticker_path", None), create=True)
    bundle = _require_runtime_bundle()
    sticker_runtime = bundle.reply_processor_deps.runtime

    labeler = StickerLabeler(
        sticker_dir,
        logger=logger,
        concurrency=max(1, int(getattr(plugin_config, "personification_labeler_concurrency", 3))),
    )
    if getattr(plugin_config, "personification_labeler_enabled", True):
        route_summary = resolve_global_fallback_provider(plugin_config, logger, warn=True)
        if sticker_runtime is not None:
            await labeler.scan_on_startup(sticker_runtime)
            _sticker_labeler_observer = start_watchdog(
                sticker_dir,
                sticker_runtime,
                logger,
                concurrency=max(1, int(getattr(plugin_config, "personification_labeler_concurrency", 3))),
                loop=asyncio.get_running_loop(),
            )
            if route_summary is not None:
                logger.info(
                    "[sticker labeler] using main-first route with fallback "
                    f"{format_provider_summary(route_summary.provider)}"
                )
            return

    await labeler.legacy_scan()


@get_driver().on_startup
async def _init_plugin_knowledge() -> None:
    global _knowledge_build_task

    bundle = _require_runtime_bundle()
    knowledge_store = bundle.reply_processor_deps.runtime.knowledge_store
    if knowledge_store is None:
        logger.warning("[plugin_knowledge] knowledge_store 未初始化，跳过后台构建")
        return

    result = await maybe_start_plugin_knowledge_builder(
        plugin_config=plugin_config,
        tool_caller=bundle.reply_processor_deps.runtime.agent_tool_caller,
        knowledge_store=knowledge_store,
        logger=logger,
        get_knowledge_build_task=_get_knowledge_build_task,
        set_knowledge_build_task=_set_knowledge_build_task,
        trigger="startup",
    )
    if result.get("started"):
        logger.info("[plugin_knowledge] 知识库后台构建已启动")
    else:
        logger.info(
            f"[plugin_knowledge] 启动检查完成，未启动构建 result={result.get('result')} reasons={result.get('reasons')}"
        )


@get_driver().on_shutdown
async def _close_personification_runtime() -> None:
    global _sticker_labeler_observer, _knowledge_build_task, _visual_probe_task, runtime_bundle
    if _sticker_labeler_observer is not None:
        _sticker_labeler_observer.stop()
        _sticker_labeler_observer.join()
        _sticker_labeler_observer = None
    if _visual_probe_task is not None and not _visual_probe_task.done():
        _visual_probe_task.cancel()
        try:
            await _visual_probe_task
        except asyncio.CancelledError:
            pass
    _visual_probe_task = None
    await stop_plugin_knowledge_builder(
        logger=logger,
        knowledge_store=(
            runtime_bundle.reply_processor_deps.runtime.knowledge_store
            if runtime_bundle is not None
            else None
        ),
        get_knowledge_build_task=_get_knowledge_build_task,
        set_knowledge_build_task=_set_knowledge_build_task,
        enabled=bool(getattr(plugin_config, "personification_plugin_knowledge_build_enabled", False)),
        trigger="shutdown",
        result="shutdown",
        reasons=["shutdown"],
    )
    _knowledge_build_task = None
    if runtime_bundle is not None and getattr(runtime_bundle, "background_intelligence", None) is not None:
        try:
            await runtime_bundle.background_intelligence.close()
        except Exception:
            pass
    runtime_bundle = None
    await close_shared_http_client(logger=logger)
