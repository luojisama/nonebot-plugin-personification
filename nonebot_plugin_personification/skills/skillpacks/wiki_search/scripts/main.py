from __future__ import annotations

import json
from typing import Any

import httpx

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from . import impl


class _SilentLogger:
    def debug(self, _msg: str) -> None:
        return None


def _parse_fandom_mapping(raw_value: Any) -> dict[str, str]:
    if isinstance(raw_value, dict):
        return {
            str(key).strip(): str(value).strip()
            for key, value in raw_value.items()
            if str(key).strip() and str(value).strip()
        }
    text = str(raw_value or "").strip()
    if not text:
        return {}
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in loaded.items()
        if str(key).strip() and str(value).strip()
    }


def resolve_wiki_runtime_config(plugin_config: Any) -> tuple[bool, bool, dict[str, str]]:
    wiki_enabled = bool(getattr(plugin_config, "personification_wiki_enabled", True))
    fandom_enabled = bool(getattr(plugin_config, "personification_wiki_fandom_enabled", True))
    fandom_wikis = (
        _parse_fandom_mapping(getattr(plugin_config, "personification_fandom_wikis", None))
        if fandom_enabled else {}
    )
    return wiki_enabled, fandom_enabled, fandom_wikis


def build_tools(runtime):
    logger = getattr(runtime, "logger", None) or _SilentLogger()
    plugin_config = getattr(runtime, "plugin_config", None)
    shared_client = getattr(runtime, "http_client", None)

    wiki_enabled, _fandom_enabled, extra_fandom_wikis = resolve_wiki_runtime_config(plugin_config)

    async def _with_client(callback):
        if shared_client is not None:
            return await callback(shared_client)
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            return await callback(http_client)

    async def _wiki_lookup_handler(query: str) -> str:
        async def _call(http_client: httpx.AsyncClient) -> str:
            return await impl.wiki_lookup(
                query,
                extra_fandom_wikis=extra_fandom_wikis,
                http_client=http_client,
                logger=logger,
            )

        return await _with_client(_call)

    return [
        AgentTool(
            name="wiki_lookup",
            description=(
                "查询维基百科、萌娘百科和可选 Fandom Wiki，适合游戏、动漫、角色、作品设定、世界观、术语、条目资料。"
                "既适合精确角色或条目查询，也适合泛话题、集合型问题。"
                "工具内部会自动处理候选搜索、精确页摘要或多候选摘要，不要求先把 query 拆成页面名。"
                "如果 Wiki 没有足够可靠的结果，会直接返回不确定。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "原始查询文本",
                    }
                },
                "required": ["query"],
            },
            handler=_wiki_lookup_handler,
            enabled=lambda: wiki_enabled,
        )
    ]

