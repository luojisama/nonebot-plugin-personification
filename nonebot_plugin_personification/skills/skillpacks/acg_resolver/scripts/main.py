from __future__ import annotations

from types import SimpleNamespace

from plugin.personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


async def run(
    query: str,
    image_context: bool = False,
    images: list[str] | None = None,
    visual_hints: dict | None = None,
) -> str:
    runtime = SkillRuntime(plugin_config=SimpleNamespace(), logger=None, get_now=lambda: None)
    return await impl.resolve_acg_entity(
        runtime=runtime,
        query=query,
        image_context=image_context,
        images=images,
        visual_hints=visual_hints,
    )


def build_tools(runtime: SkillRuntime):
    return [impl.build_resolver_tool(runtime)]
