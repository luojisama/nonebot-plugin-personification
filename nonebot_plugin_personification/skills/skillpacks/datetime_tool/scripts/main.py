from __future__ import annotations

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run(timezone: str = "Asia/Shanghai") -> str:
    tz = str(timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    return impl.get_current_datetime_info(timezone_name=tz)


def build_tools(runtime: SkillRuntime):
    timezone_name = str(getattr(runtime.plugin_config, "personification_timezone", "Asia/Shanghai") or "Asia/Shanghai")
    return [impl.build_datetime_tool(timezone_name=timezone_name)]

