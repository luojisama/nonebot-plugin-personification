from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nonebot_plugin_personification.core.web_grounding import do_web_search
from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


SKILL_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = SKILL_ROOT.parent


class _SkillLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None


async def run(query: str) -> str:
    q = str(query or "").strip()
    if not q:
        return "请提供搜索关键词。"
    try:
        config = impl.load_web_search_config(SKILLS_ROOT)
        prefix = str(config.get("search_prompt_prefix", "") or "").strip()
        final_query = f"{prefix} {q}".strip() if prefix else q
        return await do_web_search(final_query, get_now=lambda: datetime.now(), logger=_SkillLogger())
    except Exception as e:
        return f"联网搜索失败: {e}"


def build_tools(runtime: SkillRuntime):
    skills_root_raw = getattr(runtime.plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None
    return [
        impl.build_web_search_tool(
            skills_root=skills_root,
            get_now=runtime.get_now,
            logger=runtime.logger,
            plugin_config=runtime.plugin_config,
        )
    ]

