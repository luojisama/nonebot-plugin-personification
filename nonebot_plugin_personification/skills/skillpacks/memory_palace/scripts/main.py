from __future__ import annotations

from types import SimpleNamespace

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run(query: str, scope: str = "auto") -> str:
    runtime = SkillRuntime(plugin_config=SimpleNamespace(), logger=None, get_now=lambda: None)
    return await impl.recall_memory(runtime=runtime, query=query, scope=scope)


def build_tools(runtime: SkillRuntime):
    return [impl.build_memory_recall_tool(runtime)]

