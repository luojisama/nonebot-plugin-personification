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
        'Install it in the active venv first.'
    ) from e

try:
    require("nonebot_plugin_localstore")
except Exception as e:
    raise RuntimeError(
        'Cannot load required plugin "nonebot_plugin_localstore". '
        'Install it in the active venv first.'
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
from .core.qzone_startup import refresh_qzone_cookie_on_available_bot
from .core.runtime_state import close_shared_http_client
from .core.runtime_performance import sample_event_loop_lag
from .core.runtime_task_supervisor import runtime_task_supervisor
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
from .handlers.runtime_commands import check_git_update_available, perform_git_pull
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
    runtime_task_supervisor.start("startup.visual_probe", _run_visual_probe_background)


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
    runtime_task_supervisor.start("startup.llm_warmup", _warmup_llm_connection)


async def _refresh_qzone_cookie_background() -> None:
    bundle = _require_runtime_bundle()
    await refresh_qzone_cookie_on_available_bot(
        enabled=bool(getattr(plugin_config, "personification_qzone_enabled", False)),
        get_bots=get_bots,
        update_qzone_cookie=bundle.update_qzone_cookie,
        logger=logger,
    )


def _start_qzone_cookie_refresh_background() -> None:
    runtime_task_supervisor.start("startup.qzone_cookie_refresh", _refresh_qzone_cookie_background)


@get_driver().on_startup
async def _init_personification_runtime() -> None:
    global runtime_bundle, flow_handles, job_handles, matcher_handles
    global persona_matcher_handles, personification_rule, poke_rule, poke_notice_rule
    global check_proactive_messaging

    if runtime_bundle is not None:
        return

    runtime_task_supervisor.configure(logger=logger)
    runtime_task_supervisor.start("runtime.event_loop_lag", sample_event_loop_lag)
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
    runtime_bundle.qzone_social_scan = job_handles.get("qzone_social_scan")
    runtime_bundle.qzone_inbound_poll = job_handles.get("qzone_inbound_poll")
    runtime_bundle.qzone_generate_post = job_handles.get("generate_ai_diary")
    _start_qzone_cookie_refresh_background()

    if bool(getattr(plugin_config, "personification_git_auto_update", False)):
        _git_update_interval = max(
            10,
            int(getattr(plugin_config, "personification_git_auto_update_interval", 60) or 60),
        )

        async def _auto_git_update_job() -> None:
            try:
                has_update, _local, _remote = await check_git_update_available()
                if not has_update:
                    return
                ok, msg = await perform_git_pull()
                if ok:
                    logger.info(f"[git_auto_update] 已自动更新拟人插件：{msg}")
                    _bots = get_bots()
                    for _bot in _bots.values():
                        for _su in superusers:
                            try:
                                await _bot.send_private_msg(
                                    user_id=int(_su),
                                    message=f"✅ 拟人插件已自动更新，请重启 Bot 生效。\n{msg}",
                                )
                            except Exception:
                                pass
                        break
                else:
                    logger.warning(f"[git_auto_update] 自动更新失败：{msg}")
            except Exception as _exc:
                logger.warning(f"[git_auto_update] 检查或拉取失败：{_exc}")

        try:
            scheduler.add_job(
                _auto_git_update_job,
                "interval",
                minutes=_git_update_interval,
                id="personification_git_auto_update",
                replace_existing=True,
            )
            logger.info(f"拟人插件：已注册 git 自动更新任务，间隔 {_git_update_interval} 分钟")
        except Exception as _e:
            logger.warning(f"拟人插件：注册 git 自动更新任务失败：{_e}")

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
async def _apply_personification_image_host_allowlist() -> None:
    try:
        from .handlers.reply_pipeline.pipeline_sticker import set_image_host_allowlist

        raw = getattr(plugin_config, "personification_image_host_allowlist", []) or []
        set_image_host_allowlist(raw)
        if raw:
            logger.info(f"[image_safety] 已加载用户图片域名白名单: {list(raw)}")
    except Exception as exc:
        logger.warning(f"[image_safety] 加载图片域名白名单失败: {exc}")


@get_driver().on_startup
async def _register_personification_group_knowledge() -> None:
    bundle = _require_runtime_bundle()
    try:
        from .core.group_knowledge_autobuild import (
            register_group_knowledge_autobuild_job,
            register_propose_group_knowledge_tool,
        )
        from .core.group_style_autobuild import register_group_style_autobuild_job
    except Exception as exc:
        logger.warning(f"[group_knowledge] 加载失败：{exc}")
        return
    registry = getattr(bundle, "tool_registry", None)
    memory_store = getattr(bundle, "memory_store", None)
    if registry is not None and memory_store is not None:
        register_propose_group_knowledge_tool(
            registry=registry,
            memory_store=memory_store,
            logger=logger,
        )
    deps = getattr(bundle, "reply_processor_deps", None)
    runtime_inner = getattr(deps, "runtime", None) if deps is not None else None
    tool_caller = getattr(runtime_inner, "agent_tool_caller", None) if runtime_inner is not None else None
    if memory_store is not None and tool_caller is not None:
        register_group_knowledge_autobuild_job(
            scheduler=scheduler,
            plugin_config=plugin_config,
            memory_store=memory_store,
            tool_caller=tool_caller,
            logger=logger,
        )
        register_group_style_autobuild_job(
            scheduler=scheduler,
            plugin_config=plugin_config,
            memory_store=memory_store,
            tool_caller=tool_caller,
            logger=logger,
        )
    try:
        from .core.memory_summarizer import register_memory_summarizer_jobs

        register_memory_summarizer_jobs(
            scheduler=scheduler,
            plugin_config=plugin_config,
            memory_store=memory_store,
            tool_caller=tool_caller,
            logger=logger,
        )
    except Exception as exc:
        logger.warning(f"[memory_summarizer] 注册失败：{exc}")


@get_driver().on_startup
async def _install_personification_webui() -> None:
    from .webui import install_webui
    from .core.admin_acl import load_plugin_admins
    from .core.notify import startup_notify_admins

    installed = install_webui(
        plugin_config=plugin_config,
        superusers=superusers,
        get_bots=get_bots,
        logger=logger,
        runtime_bundle=runtime_bundle,
    )
    if not installed:
        return
    # 启动期清理过期设备 token 与旧审计日志
    try:
        from .core import plugin_runtime_logs, webui_audit_log, webui_auth_store

        pruned = webui_auth_store.prune_expired_devices()
        if pruned:
            logger.info(f"[webui] 启动期清理过期设备 token: {pruned} 条")
        scrubbed = webui_audit_log.scrub_sensitive_details()
        if scrubbed:
            logger.warning(f"[webui] 启动期清洗历史审计敏感字段: {scrubbed} 条")
        scrubbed_logs = plugin_runtime_logs.scrub_sensitive_entries()
        if scrubbed_logs:
            logger.warning(f"[webui] 启动期清洗历史运行日志敏感字段: {scrubbed_logs} 条")
        webui_audit_log.prune_old_entries()
    except Exception as exc:
        logger.warning(f"[webui] 启动期清理失败：{exc}")

    # 启动后台预热一次功能体检，使 WebUI 打开即有结果（不阻塞启动）
    try:
        from .core.diagnostics import warm_diagnostics

        async def _warm_health() -> None:
            await asyncio.sleep(20)  # 等 bot 连接与运行时就绪
            await warm_diagnostics(
                plugin_config=plugin_config, bundle=runtime_bundle,
                superusers=set(superusers or set()), get_bots=get_bots, logger=logger,
            )

        runtime_task_supervisor.start("startup.health_warmup", _warm_health)
    except Exception as exc:
        logger.debug(f"[webui] 体检预热调度失败：{exc}")

    driver_config = get_driver().config
    host = str(getattr(driver_config, "host", "") or "127.0.0.1")
    port = int(getattr(driver_config, "port", 8080) or 8080)
    if host in {"0.0.0.0", "::"}:
        display_host = "<本机或局域网 IP>"
    else:
        display_host = host
    webui_url = f"http://{display_host}:{port}/personification/"
    logger.info(f"拟人插件 WebUI: {webui_url}（监听 {host}:{port}）")
    try:
        notify_message = (
            "【拟人插件 WebUI】已启动\n"
            f"访问地址：{webui_url}\n"
            "使用 QQ 号登录，验证码会通过本对话推送。"
        )
        await startup_notify_admins(
            get_bots=get_bots,
            superusers=superusers,
            plugin_admins=load_plugin_admins(),
            message=notify_message,
        )
    except Exception as exc:
        logger.warning(f"拟人插件 WebUI 启动通知失败：{exc}")


@get_driver().on_startup
async def _restore_user_tasks() -> None:
    from .core.tasks_service import restore_tasks_on_startup
    from .core.qq_outbound import build_outbound_context
    from .core.services.agent_runtime import guard_scheduled_user_task_message

    bundle = _require_runtime_bundle()
    qq_outbound_ledger = bundle.qq_outbound_ledger
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
        message = guard_scheduled_user_task_message(task, message, logger=logger)
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
                if qq_outbound_ledger is None:
                    await bot.send_private_msg(user_id=int(user_id), message=message)
                    task["last_status"] = "sent"
                else:
                    outbound_context = build_outbound_context(
                        bot=bot,
                        event=types.SimpleNamespace(user_id=user_id),
                        surface="scheduled_user_task",
                        user_target=str(user_id),
                    )
                    receipt = await qq_outbound_ledger.dispatch(
                        outbound_context,
                        message,
                        lambda: bot.send_private_msg(user_id=int(user_id), message=message),
                    )
                    task["last_status"] = receipt.status
                return
            except Exception as e:
                task["last_status"] = "unknown" if qq_outbound_ledger is not None else "failed"
                logger.warning(f"[user_tasks] 任务消息发送失败 user={user_id}: {e}")
                if qq_outbound_ledger is not None:
                    return
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
    try:
        from .core.tool_health import schedule_tool_health_probes

        schedule_tool_health_probes(
            registry=registry,
            scheduler=scheduler,
            logger=logger,
            startup_delay_seconds=10.0,
            timeout_seconds=20.0,
        )
    except Exception as exc:
        logger.warning(f"[tool_health] 启动工具巡检失败：{exc}")
    try:
        from .core.mcp_management import get_mcp_manager
        from .webui.app import get_runtime_context

        await get_mcp_manager(get_runtime_context()).reload()
    except Exception as exc:
        logger.warning(f"[mcp] 托管安装恢复失败：{type(exc).__name__}")


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


@get_driver().on_startup
async def _setup_social_intelligence() -> None:
    """注册主动社交场景到 APScheduler。默认总开关关闭，开了才真触发。"""
    bundle = _require_runtime_bundle()
    try:
        from .flows.social_intelligence import (
            SocialContext,
            setup_social_intelligence_jobs,
        )

        ctx = SocialContext(
            plugin_config=plugin_config,
            logger=logger,
            get_bots=get_bots,
            get_whitelisted_groups=bundle._get_whitelisted_groups,
            tool_caller=bundle.reply_processor_deps.runtime.agent_tool_caller,
            persona_store=bundle.persona_store,
            data_dir=get_personification_data_dir(plugin_config),
            get_now=get_current_local_time,
            load_prompt=bundle.load_prompt,
            tool_registry=bundle.reply_processor_deps.runtime.tool_registry,
            agent_max_steps=int(getattr(plugin_config, "personification_agent_max_steps", 10)),
            qq_outbound_ledger=bundle.qq_outbound_ledger,
        )
        registered = setup_social_intelligence_jobs(scheduler=scheduler, ctx=ctx)
        if registered > 0:
            logger.info(f"[social] 已注册 {registered} 个主动社交触发器")
        else:
            logger.info("[social] 未注册任何主动社交触发器（开关关闭或场景被禁用）")
    except Exception as exc:
        logger.error(f"[social] setup_social_intelligence_jobs 失败：{exc}")


@get_driver().on_shutdown
async def _close_personification_runtime() -> None:
    global _sticker_labeler_observer, _knowledge_build_task, runtime_bundle
    if runtime_bundle is not None:
        scoped_profile_service = getattr(runtime_bundle, "scoped_profile_service", None)
        if scoped_profile_service is not None:
            try:
                await scoped_profile_service.close()
            except Exception as exc:
                logger.debug(f"[scoped_profile] shutdown cleanup failed: {exc}")
    if _sticker_labeler_observer is not None:
        _sticker_labeler_observer.stop()
        _sticker_labeler_observer.join()
        _sticker_labeler_observer = None
    await runtime_task_supervisor.shutdown(timeout=5.0)
    from .core.qzone_auth import qzone_login_manager

    await qzone_login_manager.shutdown()
    from .core.mcp_management import shutdown_mcp_managers

    await shutdown_mcp_managers()
    from .core.gemini_web_service import shutdown_gemini_web_services
    from .core.mimo_web_asr_service import shutdown_mimo_web_asr_services

    await shutdown_gemini_web_services()
    await shutdown_mimo_web_asr_services()
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
