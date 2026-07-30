from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx

from plugin.personification.agent.tool_registry import AgentTool


WEATHER_DESCRIPTION = """查询指定城市的天气信息，支持当前天气和未来 1-16 天预报。
适合场景：用户询问天气、气温、会不会下雨、未来几天/半个月天气、出行穿衣，或聊到某城市气候。
城市名支持中文，如"北京""昆明""魔都"（魔都=上海）。
如果用户没有明确说城市，先根据当前用户档案、已注入上下文或记忆工具确认可靠地点；没有可靠地点时不要猜城市，应先自然追问。
days 表示预报天数；当前天气传 1，问未来几天/这周/半个月时传对应天数，最大 16。
查询结果用角色口吻自然融入对话，比如"今天26度，有点热"，
不要说"根据天气API"或直接复读数字，不要用 markdown 列表。"""

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

OPEN_METEO_WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "毛毛雨",
    53: "小雨",
    55: "中雨",
    56: "冻毛毛雨",
    57: "冻雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "阵雨",
    81: "强阵雨",
    82: "暴雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴小冰雹",
    99: "雷暴伴大冰雹",
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


def coerce_forecast_days(days: Any = 1) -> int:
    try:
        value = int(days or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, 16))


async def fetch_weather(city: str, days: Any = 1) -> str:
    forecast_days = coerce_forecast_days(days)
    if forecast_days > 1:
        return await _fetch_weather_open_meteo_forecast(city, forecast_days)

    wttr_error = ""
    try:
        return await _fetch_weather_wttr(city)
    except Exception as e:
        wttr_error = str(e)

    try:
        return await _fetch_weather_open_meteo(city)
    except Exception as e:
        if wttr_error:
            raise RuntimeError(f"wttr={wttr_error}; open-meteo={e}") from e
        raise


async def _fetch_weather_wttr(city: str) -> str:
    url = f"https://wttr.in/{quote(city)}?format=3&lang=zh"
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text.strip()


async def _geocode_city(client: httpx.AsyncClient, city: str) -> dict[str, Any]:
    geo_resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={
            "q": city,
            "format": "jsonv2",
            "limit": 1,
            "accept-language": "zh-CN",
        },
        headers={
            "User-Agent": "personification-weather-skill/1.0",
        },
    )
    geo_resp.raise_for_status()
    geo_data = geo_resp.json()
    results = geo_data if isinstance(geo_data, list) else []
    if not results:
        raise RuntimeError(f"未找到城市 {city}")
    first = results[0] if isinstance(results[0], dict) else {}
    lat = first.get("latitude")
    lon = first.get("longitude")
    if lat is None:
        lat = first.get("lat")
    if lon is None:
        lon = first.get("lon")
    if lat is None or lon is None:
        raise RuntimeError(f"城市坐标缺失: {city}")
    return first


async def _fetch_weather_open_meteo(city: str) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        first = await _geocode_city(client, city)
        lat = first.get("latitude")
        lon = first.get("longitude")
        if lat is None:
            lat = first.get("lat")
        if lon is None:
            lon = first.get("lon")
        if lat is None or lon is None:
            raise RuntimeError(f"城市坐标缺失: {city}")

        weather_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code",
                "timezone": "auto",
            },
        )
        weather_resp.raise_for_status()
        weather_data = weather_resp.json()
        current = weather_data.get("current", {}) if isinstance(weather_data, dict) else {}
        if not isinstance(current, dict) or not current:
            raise RuntimeError("天气数据为空")

        display_name = str(first.get("name") or first.get("display_name") or city).strip() or city

        temp = current.get("temperature_2m")
        apparent = current.get("apparent_temperature")
        humidity = current.get("relative_humidity_2m")
        code = int(current.get("weather_code", -1) or -1)
        desc = OPEN_METEO_WEATHER_CODES.get(code, "天气未知")

        parts = [f"{display_name}: {temp}°C" if temp is not None else f"{display_name}: 温度未知", desc]
        if apparent is not None:
            parts.append(f"体感{apparent}°C")
        if humidity is not None:
            parts.append(f"湿度{humidity}%")
        return " ".join(str(part) for part in parts if str(part).strip())


def _format_forecast_date(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return f"{parsed.month}月{parsed.day}日"
    except Exception:
        return text


def _is_wet_weather(code: int, precipitation: Any, probability: Any) -> bool:
    try:
        precip_value = float(precipitation or 0)
    except (TypeError, ValueError):
        precip_value = 0.0
    try:
        probability_value = float(probability or 0)
    except (TypeError, ValueError):
        probability_value = 0.0
    return (
        precip_value > 0.1
        or probability_value >= 50
        or code in {
            51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
            71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
        }
    )


def _is_heavy_weather(code: int, precipitation: Any, probability: Any) -> bool:
    try:
        precip_value = float(precipitation or 0)
    except (TypeError, ValueError):
        precip_value = 0.0
    try:
        probability_value = float(probability or 0)
    except (TypeError, ValueError):
        probability_value = 0.0
    return code in {65, 67, 80, 81, 82, 95, 96, 99} or precip_value >= 10 or probability_value >= 80


async def _fetch_weather_open_meteo_forecast(city: str, days: int) -> str:
    async with httpx.AsyncClient(timeout=12.0) as client:
        first = await _geocode_city(client, city)
        lat = first.get("latitude") if first.get("latitude") is not None else first.get("lat")
        lon = first.get("longitude") if first.get("longitude") is not None else first.get("lon")
        if lat is None or lon is None:
            raise RuntimeError(f"城市坐标缺失: {city}")
        display_name = str(first.get("name") or first.get("display_name") or city).strip() or city
        weather_resp = await client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": days,
            },
        )
        weather_resp.raise_for_status()
        weather_data = weather_resp.json()
        daily = weather_data.get("daily", {}) if isinstance(weather_data, dict) else {}
        if not isinstance(daily, dict) or not daily:
            raise RuntimeError("天气预报数据为空")

        dates = list(daily.get("time") or [])[:days]
        codes = list(daily.get("weather_code") or [])[:days]
        temp_max = list(daily.get("temperature_2m_max") or [])[:days]
        temp_min = list(daily.get("temperature_2m_min") or [])[:days]
        precipitation = list(daily.get("precipitation_sum") or [])[:days]
        probability = list(daily.get("precipitation_probability_max") or [])[:days]
        count = min(len(dates), days)
        if count <= 0:
            raise RuntimeError("天气预报数据为空")

        wet_dates: list[str] = []
        heavy_dates: list[str] = []
        descriptions: list[str] = []
        lows: list[float] = []
        highs: list[float] = []
        for index in range(count):
            try:
                code = int(codes[index] if index < len(codes) else -1)
            except (TypeError, ValueError):
                code = -1
            date_label = _format_forecast_date(dates[index])
            precip = precipitation[index] if index < len(precipitation) else 0
            prob = probability[index] if index < len(probability) else 0
            if _is_wet_weather(code, precip, prob):
                wet_dates.append(date_label)
            if _is_heavy_weather(code, precip, prob):
                heavy_dates.append(date_label)
            desc = OPEN_METEO_WEATHER_CODES.get(code, "")
            if desc and desc not in descriptions:
                descriptions.append(desc)
            try:
                lows.append(float(temp_min[index]))
            except Exception:
                pass
            try:
                highs.append(float(temp_max[index]))
            except Exception:
                pass

        parts = [f"{display_name}未来{count}天"]
        if wet_dates:
            shown = "、".join(wet_dates[:6])
            suffix = "等" if len(wet_dates) > 6 else ""
            parts.append(f"有雨大概{len(wet_dates)}天，集中在{shown}{suffix}")
        else:
            parts.append("降雨不多")
        if lows and highs:
            parts.append(f"气温约{min(lows):.0f}-{max(highs):.0f}度")
        if heavy_dates:
            shown_heavy = "、".join(heavy_dates[:4])
            suffix = "等" if len(heavy_dates) > 4 else ""
            parts.append(f"{shown_heavy}{suffix}可能雨势更明显")
        elif descriptions:
            parts.append("主要是" + "、".join(descriptions[:3]))
        return "；".join(parts) + "。"


def build_weather_tool(skills_root: Optional[Path], logger: Any) -> AgentTool:
    config = load_weather_config(skills_root)

    async def _handler(city: str, days: int = 1) -> str:
        resolved_city = resolve_city_alias(city, config)
        forecast_days = coerce_forecast_days(days)
        try:
            return await fetch_weather(resolved_city, forecast_days)
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
                },
                "days": {
                    "type": "integer",
                    "description": "预报天数，当前天气用1；未来几天/这周/半个月用对应天数，范围1到16",
                    "default": 1,
                    "minimum": 1,
                    "maximum": 16,
                }
            },
            "required": ["city"],
        },
        handler=_handler,
        metadata={
            "intent_tags": ["lookup", "weather"],
            "evidence_kind": "weather",
            "requires_network": True,
            "latency_class": "normal",
        },
    )
