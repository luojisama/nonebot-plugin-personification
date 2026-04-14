import asyncio
from importlib import import_module
from pathlib import Path

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
    scheduler = import_module("nonebot_plugin_apscheduler").scheduler
except Exception as e:
    raise RuntimeError(
        'Cannot load required plugin "nonebot_plugin_apscheduler". '
        'Install it in the active venv first, for example: '
        '"F:\\bot\\shirotest\\.venv\\Scripts\\python.exe -m pip install nonebot-plugin-apscheduler".'
    ) from e

try:
    require("nonebot_plugin_htmlrender")
    md_to_pic = getattr(import_module("nonebot_plugin_htmlrender"), "md_to_pic", None)
except Exception as e:
    logger.warning(f'拟人插件：加载 "nonebot_plugin_htmlrender" 失败，渲染能力将降级。{e}')
    md_to_pic = None

from .config import Config
from .core.file_sender import build_file_sender
from .core.plugin_meta import build_plugin_metadata
from .core.plugin_runtime import build_plugin_runtime
from .core.runtime_state import close_shared_http_client
from .flows import setup_flows
from .handlers import setup_all_matchers
from .handlers.persona_commands import setup_persona_matchers
from .jobs import setup_jobs
from .schedule import get_current_local_time
from .agent.inner_state import get_personification_data_dir
from .skill_runtime.runtime_api import SkillRuntime
from .skills.skillpacks.sticker_labeler.scripts.impl import StickerLabeler, start_watchdog
from .skills.skillpacks.tool_caller.scripts.impl import build_tool_caller
from .skills.skillpacks.vision_caller.scripts.impl import build_vision_caller
from .utils import is_group_whitelisted

plugin_config = get_plugin_config(Config)
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
            start_knowledge_builder=import_module(
                ".core.knowledge_builder",
                __package__,
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
            tool_caller = build_tool_caller(plugin_config)
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

    sticker_path = getattr(plugin_config, "personification_sticker_path", None)
    if not sticker_path:
        return

    labeler = StickerLabeler(
        Path(sticker_path),
        logger=logger,
        concurrency=max(1, int(getattr(plugin_config, "personification_labeler_concurrency", 3))),
    )
    vision_caller = None
    if getattr(plugin_config, "personification_labeler_enabled", True):
        vision_caller = build_vision_caller(plugin_config)
        if vision_caller is not None:
            await labeler.scan_on_startup(vision_caller)
            _sticker_labeler_observer = start_watchdog(
                Path(sticker_path),
                vision_caller,
                logger,
                concurrency=max(1, int(getattr(plugin_config, "personification_labeler_concurrency", 3))),
                loop=asyncio.get_running_loop(),
            )
            return

    await labeler.legacy_scan()


@get_driver().on_startup
async def _init_plugin_knowledge() -> None:
    global _knowledge_build_task

    from .core.knowledge_builder import start_knowledge_builder

    bundle = _require_runtime_bundle()
    knowledge_store = bundle.reply_processor_deps.runtime.knowledge_store
    if knowledge_store is None:
        logger.warning("[plugin_knowledge] knowledge_store 未初始化，跳过后台构建")
        return

    _knowledge_build_task = start_knowledge_builder(
        plugin_config=plugin_config,
        tool_caller=bundle.reply_processor_deps.runtime.agent_tool_caller,
        knowledge_store=knowledge_store,
        logger=logger,
    )
    logger.info("[plugin_knowledge] 知识库后台构建已启动")


@get_driver().on_shutdown
async def _close_personification_runtime() -> None:
    global _sticker_labeler_observer, _knowledge_build_task, runtime_bundle
    if _sticker_labeler_observer is not None:
        _sticker_labeler_observer.stop()
        _sticker_labeler_observer.join()
        _sticker_labeler_observer = None
    if _knowledge_build_task is not None and not _knowledge_build_task.done():
        _knowledge_build_task.cancel()
        try:
            await _knowledge_build_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        _knowledge_build_task = None
    if runtime_bundle is not None and getattr(runtime_bundle, "background_intelligence", None) is not None:
        try:
            await runtime_bundle.background_intelligence.close()
        except Exception:
            pass
    runtime_bundle = None
    await close_shared_http_client(logger=logger)
