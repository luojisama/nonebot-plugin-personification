from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from ..agent.tool_registry import AgentTool


WEATHER_DESCRIPTION = """查询指定城市的实时天气信息，返回温度、天气状况、体感温度、湿度。
适合场景：用户询问天气、聊到出行穿衣、提到某城市的气候。
城市名支持中文，如"北京""昆明""魔都"（魔都=上海）。
查询结果用角色口吻自然融入对话，比如"刚看了下，今天26度有点热"，
不要说"根据天气API"或直接复读数字。"""

WEATHER_RESULT_PROMPT = """当前天气数据：{weather_data}

请用角色口吻把天气信息自然地说出来，结合当前对话语境决定重点说什么。
例如：
- 如果对方要出门，重点说是否需要带伞、穿什么衣服
- 如果只是随口一问，轻描淡写说温度和天气状况即可
- 不要像播报天气预报一样逐项列举
字数控制在20字以内。"""

DEFAULT_WEATHER_CONFIG = {
    "aliases": {
        "魔都": "上海",
        "帝都": "北京",
        "鹏城": "深圳",
        "羊城": "广州",
        "春城": "昆明",
    },
    "unit": "auto",
}


def load_weather_config(skills_root: Optional[Path]) -> dict:
    config = {
        "aliases": dict(DEFAULT_WEATHER_CONFIG["aliases"]),
        "unit": DEFAULT_WEATHER_CONFIG["unit"],
    }
    if skills_root is None:
        return config

    config_path = Path(skills_root) / "weather" / "config.yaml"
    if not config_path.exists():
        return config

    try:
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return config

    if not isinstance(loaded, dict):
        return config

    aliases = loaded.get("aliases")
    if isinstance(aliases, dict):
        config["aliases"].update({str(k): str(v) for k, v in aliases.items()})
    unit = loaded.get("unit")
    if isinstance(unit, str) and unit.strip():
        config["unit"] = unit.strip()
    return config


def resolve_city_alias(city: str, config: Optional[dict] = None) -> str:
    resolved = dict(DEFAULT_WEATHER_CONFIG["aliases"])
    if isinstance(config, dict):
        aliases = config.get("aliases")
        if isinstance(aliases, dict):
            resolved.update({str(k): str(v) for k, v in aliases.items()})
    return resolved.get(city, city)


async def fetch_weather(city: str) -> str:
    url = f"https://wttr.in/{quote(city)}?format=3&lang=zh"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text.strip()


def build_weather_tool(skills_root: Optional[Path], logger: Any) -> AgentTool:
    config = load_weather_config(skills_root)

    async def _handler(city: str) -> str:
        resolved_city = resolve_city_alias(city, config)
        try:
            return await fetch_weather(resolved_city)
        except Exception as e:
            logger.warning(f"[weather] query failed for {resolved_city}: {e}")
            return f"{resolved_city} 天气查询失败"

    return AgentTool(
        name="weather",
        description=WEATHER_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "要查询天气的城市名，支持中文别名",
                }
            },
            "required": ["city"],
        },
        handler=_handler,
    )
