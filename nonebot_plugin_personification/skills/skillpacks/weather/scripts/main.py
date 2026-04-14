from __future__ import annotations

from pathlib import Path

from nonebot_plugin_personification.skill_runtime.runtime_api import SkillRuntime
from . import impl


SKILL_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = SKILL_ROOT.parent


async def run(city: str) -> str:
    city_name = str(city or "").strip()
    if not city_name:
        return "请提供城市名，例如：北京、上海、广州。"
    try:
        config = impl.load_weather_config(SKILLS_ROOT)
        resolved = impl.resolve_city_alias(city_name, config)
        result = await impl.fetch_weather(resolved)
        return result or f"{resolved} 天气查询失败"
    except Exception as e:
        return f"{city_name} 天气查询失败: {e}"


def build_tools(runtime: SkillRuntime):
    skills_root_raw = getattr(runtime.plugin_config, "personification_skills_path", None)
    skills_root = Path(skills_root_raw) if skills_root_raw else None
    return [impl.build_weather_tool(skills_root, runtime.logger)]

