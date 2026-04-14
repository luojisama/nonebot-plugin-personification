from __future__ import annotations

from types import SimpleNamespace

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run(query: str, images: list[str] | None = None) -> str:
    runtime = SkillRuntime(plugin_config=SimpleNamespace(), logger=None, get_now=lambda: None)
    return await impl.analyze_images(runtime=runtime, query=query, images=images)


def build_tools(runtime: SkillRuntime):
    return [impl.build_vision_tool(runtime)]

