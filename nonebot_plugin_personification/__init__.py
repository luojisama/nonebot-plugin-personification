import asyncio
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
    from nonebot_plugin_apscheduler import scheduler
except Exception as e:
    raise RuntimeError(
        'Cannot load required plugin "nonebot_plugin_apscheduler". '
        "Install it in the active venv first."
    ) from e

try:
    from nonebot_plugin_htmlrender import md_to_pic
except ImportError:
    md_to_pic = None

from .config import Config
from .core.plugin_runtime import build_plugin_runtime
from .core.runtime_state import close_shared_http_client
from .flows import setup_flows
from .handlers import setup_all_matchers
from .handlers.persona_commands import setup_persona_matchers
from .jobs import setup_jobs
from .skills.sticker_labeler import StickerLabeler, start_watchdog
from .skills.tool_caller import build_tool_caller
from .skills.vision_caller import build_vision_caller
from .utils import is_group_whitelisted

plugin_config = get_plugin_config(Config)
superusers = get_driver().config.superusers
_sticker_labeler_observer = None

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

__plugin_meta__ = runtime_bundle.plugin_meta
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
        apply_web_search_switch=flow_handles["apply_web_search_switch"],
        apply_proactive_switch=flow_handles["apply_proactive_switch"],
    )
)
globals().update(matcher_handles)


async def _persona_whitelist_rule(event: MessageEvent) -> bool:
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        return is_group_whitelisted(
            str(event.group_id),
            plugin_config.personification_whitelist,
        )
    return False


if runtime_bundle.persona_store is not None:
    persona_matcher_handles = setup_persona_matchers(
        persona_store=runtime_bundle.persona_store,
        whitelist_rule=Rule(_persona_whitelist_rule),
        logger=logger,
    )
    globals().update(persona_matcher_handles)


@get_driver().on_startup
async def _init_personification_persona_store() -> None:
    if runtime_bundle.persona_store is None:
        return
    await runtime_bundle.persona_store.load()
    logger.info("[user_persona] 画像数据已加载")


@get_driver().on_startup
async def _restore_user_tasks() -> None:
    from .agent.inner_state import get_personification_data_dir
    from .skills.user_tasks import restore_tasks_on_startup

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
                await bot.send_private_msg(
                    user_id=int(user_id),
                    message=message,
                )
                return
            except Exception:
                continue

    restore_tasks_on_startup(scheduler, data_dir, _bot_caller)
    logger.info("[user_tasks] 持久化定时任务已恢复")


@get_driver().on_startup
async def _load_custom_skills() -> None:
    skills_path = getattr(plugin_config, "personification_skills_path", None)
    if not skills_path:
        return

    from .skills.custom_loader import load_custom_skills

    skills_root = Path(skills_path)
    if not skills_root.exists():
        return

    registry = runtime_bundle.tool_registry
    if registry is None:
        return

    tool_caller = runtime_bundle.reply_processor_deps.runtime.agent_tool_caller
    if tool_caller is None:
        try:
            tool_caller = build_tool_caller(plugin_config)
        except Exception as e:
            logger.warning(f"[custom_skills] 构建自动分析 tool caller 失败: {e}")
            tool_caller = None

    await load_custom_skills(skills_root, registry, logger, tool_caller=tool_caller)
    logger.info(f"[custom_skills] 自定义 skill 已从 {skills_root} 加载")


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


@get_driver().on_shutdown
async def _close_personification_runtime() -> None:
    global _sticker_labeler_observer
    if _sticker_labeler_observer is not None:
        _sticker_labeler_observer.stop()
        _sticker_labeler_observer.join()
        _sticker_labeler_observer = None
    await close_shared_http_client(logger=logger)
