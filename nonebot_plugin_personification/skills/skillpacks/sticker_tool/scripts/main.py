from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nonebot_plugin_personification.plugin_data import get_plugin_data_dir
from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl as legacy


async def run(
    action: str,
    mood: str = "",
    context: str = "",
    proactive: bool = False,
    sticker_dir: str = "",
    user_text: str = "",
) -> str:
    act = str(action or "").strip().lower()
    if act == "select":
        target_dir = Path(sticker_dir) if sticker_dir else get_plugin_data_dir() / "stickers"
        cfg = SimpleNamespace(personification_sticker_semantic=True)
        selected = legacy.select_sticker(
            target_dir,
            mood=mood,
            context=context,
            proactive=bool(proactive),
            plugin_config=cfg,
            skills_root=Path(__file__).resolve().parents[3],
        )
        return selected or ""
    if act == "context":
        urls = legacy.get_current_image_urls()
        text = user_text or legacy.get_current_image_text()
        return f"image_urls={len(urls)} text={text}"
    return "action 可选: select, context"


def build_tools(runtime: SkillRuntime):
    skills_root_raw = getattr(runtime.plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None
    tools = []
    sticker_path = getattr(runtime.plugin_config, "personification_sticker_path", None)
    if sticker_path:
        tools.append(
            legacy.build_select_sticker_tool(
                Path(sticker_path),
                runtime.plugin_config,
                skills_root=skills_root,
            )
        )

    async def _image_web_search(query: str) -> str:
        from nonebot_plugin_personification.core.web_grounding import do_web_search

        return await do_web_search(query, get_now=runtime.get_now, logger=runtime.logger)

    tools.append(legacy.build_analyze_image_tool(runtime.vision_caller, _image_web_search))
    return tools

