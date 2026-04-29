from __future__ import annotations

from typing import Any

from ..tool_registry import ToolRegistry
from .constants import DEFAULT_AGENT_MAX_STEPS, MAX_AGENT_MAX_STEPS, MIN_AGENT_MAX_STEPS


_IMAGE_REQUIRED_TOOL_NAMES = frozenset(
    {
        "vision_analyze",
        "analyze_image",
        "understand_sticker",
    }
)
_IMAGE_GENERATION_TOOL_NAMES = frozenset({"generate_image"})
_IMAGE_GENERATION_CONTEXT_TOOL_NAMES = frozenset(
    {
        "parallel_research",
        "web_search",
        "search_web",
        "search_images",
        "collect_resources",
        "wiki_lookup",
        "resolve_acg_entity",
    }
)
_PLUGIN_LOCAL_TOOL_NAMES = frozenset(
    {
        "search_plugin_knowledge",
        "search_plugin_source",
        "list_plugins",
        "list_plugin_features",
        "get_feature_detail",
    }
)
_PLUGIN_WEB_TOOL_NAMES = frozenset(
    {
        "web_search",
        "search_web",
        "search_official_site",
        "search_github_repos",
    }
)
_ADMIN_TOOL_NAMES = frozenset(
    {
        "sticker_labeler",
        "curate_sticker_library",
        "user_persona",
        "user_tasks",
        "time_companion",
        "tool_caller",
        "vision_caller",
        "confirm_resource_request",
    }
)


def normalize_agent_max_steps(value: Any, default: int = DEFAULT_AGENT_MAX_STEPS) -> int:
    try:
        steps = int(value)
    except (TypeError, ValueError):
        steps = default
    if steps <= 0:
        steps = default
    return max(MIN_AGENT_MAX_STEPS, min(MAX_AGENT_MAX_STEPS, steps))


def schema_tool_name(schema: dict) -> str:
    function = schema.get("function", {}) if isinstance(schema, dict) else {}
    if not isinstance(function, dict):
        function = {}
    return str(function.get("name", "") or "").strip()


def select_tool_schemas(
    registry: ToolRegistry,
    *,
    has_images: bool,
    chat_intent: str = "",
    plugin_question_intent: str = "",
) -> list[dict]:
    schemas = registry.openai_schemas()
    if not schemas:
        return []
    result_schemas: list[dict]
    effective_chat_intent = str(chat_intent or "").strip()
    if effective_chat_intent == "banter":
        if not has_images:
            return []
        result_schemas = [
            schema
            for schema in schemas
            if schema_tool_name(schema) in _IMAGE_REQUIRED_TOOL_NAMES
        ]
    elif effective_chat_intent == "image_generation":
        allowed = set(_IMAGE_GENERATION_TOOL_NAMES)
        allowed.update(_IMAGE_GENERATION_CONTEXT_TOOL_NAMES)
        if has_images:
            allowed.update(_IMAGE_REQUIRED_TOOL_NAMES)
        result_schemas = [
            schema
            for schema in schemas
            if schema_tool_name(schema) in allowed
        ]
    elif effective_chat_intent == "plugin_question":
        allowed = set(_PLUGIN_LOCAL_TOOL_NAMES)
        if str(plugin_question_intent or "").strip() == "latest":
            allowed.update(_PLUGIN_WEB_TOOL_NAMES)
        if has_images:
            allowed.update(_IMAGE_REQUIRED_TOOL_NAMES)
        result_schemas = [
            schema
            for schema in schemas
            if schema_tool_name(schema) in allowed
        ]
    elif has_images:
        result_schemas = list(schemas)
    else:
        result_schemas = [
            schema
            for schema in schemas
            if schema_tool_name(schema) not in _IMAGE_REQUIRED_TOOL_NAMES
        ]
    return [
        schema
        for schema in result_schemas
        if schema_tool_name(schema) not in _ADMIN_TOOL_NAMES
    ]


def semantic_tool_guidance() -> str:
    return (
        "工具使用总原则：能直接回答就别起工具；不确定、高风险、时效性强、明显需要查证时再调用工具。"
        "插件技术问题优先本地插件知识和源码工具。"
        "用户明确要求生成图片时，必须调用 generate_image，不要只给提示词。"
        "群聊接梗场景优先像群友接话，不要为了显得聪明而滥用工具。"
    )


__all__ = [
    "DEFAULT_AGENT_MAX_STEPS",
    "MAX_AGENT_MAX_STEPS",
    "MIN_AGENT_MAX_STEPS",
    "normalize_agent_max_steps",
    "schema_tool_name",
    "select_tool_schemas",
    "semantic_tool_guidance",
]
