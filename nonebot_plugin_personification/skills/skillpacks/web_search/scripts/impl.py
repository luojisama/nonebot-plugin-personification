from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from nonebot_plugin_personification.agent.tool_registry import AgentTool
from nonebot_plugin_personification.core.web_grounding import do_web_search as do_web_search_core


WEB_SEARCH_DESCRIPTION = """快速搜索互联网并返回已整理的摘要文本。
适合使用的场景：需要实时信息（天气除外）、近期事件、事实查证、补充背景知识。
不适合使用的场景：查结构化链接列表、GitHub 仓库、官网入口、图片资源，这些应优先使用更专门的搜索工具。
搜索后，将结果消化为自然语言，用角色口吻说出，不要暴露"我搜索了"。"""

SEARCH_RESULT_FORMAT_PROMPT = """以下是搜索结果，请从中提取关键信息，用角色口吻自然地表达出来。
不要逐条列举搜索结果，不要出现"根据搜索""数据显示"等表达。
如果搜索结果与问题无关或质量很差，就用自己的知识回答，不要强行引用。

搜索结果：
{search_results}"""

DEFAULT_WEB_SEARCH_CONFIG = {
    "max_results": 5,
    "search_prompt_prefix": "",
    "blocked_domains": [],
    "result_format_prompt": "",
}


def load_web_search_config(skills_root: Optional[Path]) -> dict:
    config = dict(DEFAULT_WEB_SEARCH_CONFIG)
    if skills_root is None:
        return config

    config_path = Path(skills_root) / "web_search" / "config.yaml"
    if not config_path.exists():
        return config

    try:
        import yaml

        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return config

    if not isinstance(loaded, dict):
        return config

    for key in DEFAULT_WEB_SEARCH_CONFIG:
        if key in loaded:
            config[key] = loaded[key]
    if not isinstance(config.get("blocked_domains"), list):
        config["blocked_domains"] = []
    return config


def format_search_result_prompt(search_results: str, config: Optional[dict] = None) -> str:
    resolved = dict(DEFAULT_WEB_SEARCH_CONFIG)
    if isinstance(config, dict):
        resolved.update(config)
    template = str(resolved.get("result_format_prompt") or SEARCH_RESULT_FORMAT_PROMPT)
    return template.format(search_results=search_results)


def build_web_search_tool(
    *,
    skills_root: Optional[Path],
    get_now: Callable[[], Any],
    logger: Any,
    plugin_config: Any | None = None,
) -> AgentTool:
    config = load_web_search_config(skills_root)

    async def _handler(query: str) -> str:
        prefix = str(config.get("search_prompt_prefix", "") or "").strip()
        final_query = f"{prefix} {query}".strip() if prefix else query
        return await do_web_search_core(
            final_query,
            get_now=get_now,
            logger=logger,
        )

    return AgentTool(
        name="web_search",
        description=WEB_SEARCH_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，应简洁明确，支持中英文",
                }
            },
            "required": ["query"],
        },
        handler=_handler,
        enabled=lambda: bool(
            getattr(
                plugin_config,
                "personification_tool_web_search_enabled",
                True,
            )
        ) and str(
            getattr(plugin_config, "personification_tool_web_search_mode", "enabled") or "enabled"
        ).strip().lower() != "disabled",
    )

