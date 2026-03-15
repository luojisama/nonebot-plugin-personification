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
    from nonebot_plugin_htmlrender import md_to_pic
except ImportError:
    md_to_pic = None

from .config import Config
from .core.plugin_runtime import build_plugin_runtime
from .core.runtime_state import close_shared_http_client
from .flows import setup_flows
from .handlers import setup_all_matchers
from .jobs import setup_jobs

plugin_config = get_plugin_config(Config)
superusers = get_driver().config.superusers

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


@get_driver().on_shutdown
async def _close_personification_runtime() -> None:
    await close_shared_http_client(logger=logger)
