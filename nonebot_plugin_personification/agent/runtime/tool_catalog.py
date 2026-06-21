from __future__ import annotations

from typing import Any

from ..tool_registry import AgentTool
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
# 闲聊/接梗场景也放行的一组"轻量查证"工具：遇到看不懂的梗/专有名词/外号/分享内容时，
# 模型应先查清楚再用自己的口吻接话；runner 会用模型侧草稿审查兜底。
_LIGHTWEIGHT_LOOKUP_TOOL_NAMES = frozenset(
    {
        "web_search",
        "search_web",
        "wiki_lookup",
        "resolve_acg_entity",
        "weather",
        "memory_recall",
        "recall_user_memory",
        "recall_group_memory",
        "get_user_persona",
    }
)
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
        "invoke_plugin",
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
_NETWORK_TOOL_NAMES = frozenset(
    {
        "parallel_research",
        "web_search",
        "search_web",
        "multi_search_engine",
        "collect_resources",
        "search_images",
        "search_official_site",
        "search_github_repos",
        "wiki_lookup",
        "get_baike_entry",
        "get_daily_news",
        "get_ai_news",
        "get_trending",
        "get_history_today",
        "get_epic_games",
        "get_gold_price",
        "get_exchange_rate",
        "weather",
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
_MEMORY_TOOL_NAMES = frozenset({"memory_recall", "recall_user_memory", "recall_group_memory", "get_user_persona"})


def _coerce_tags(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item or "").strip() for item in value if str(item or "").strip()}
    return set()


def _default_tool_metadata(tool_name: str) -> dict[str, Any]:
    name = str(tool_name or "").strip()
    metadata: dict[str, Any] = {
        "intent_tags": [],
        "evidence_kind": "generic",
        "requires_network": False,
        "requires_image": False,
        "latency_class": "normal",
        "risk_level": "low",
    }
    if name in _ADMIN_TOOL_NAMES:
        metadata.update({"risk_level": "admin", "intent_tags": ["admin"]})
    if name in _IMAGE_REQUIRED_TOOL_NAMES:
        metadata.update(
            {
                "intent_tags": ["vision"],
                "evidence_kind": "visual_summary",
                "requires_image": True,
                "latency_class": "medium",
            }
        )
    if name in _IMAGE_GENERATION_TOOL_NAMES:
        metadata.update(
            {
                "intent_tags": ["image_generation"],
                "evidence_kind": "direct_media",
                "latency_class": "slow",
            }
        )
    if name in _IMAGE_GENERATION_CONTEXT_TOOL_NAMES:
        tags = _coerce_tags(metadata.get("intent_tags"))
        tags.update({"lookup", "image_generation"})
        metadata.update(
            {
                "intent_tags": sorted(tags),
                "evidence_kind": "web" if name in _NETWORK_TOOL_NAMES else "resource",
                "requires_network": name in _NETWORK_TOOL_NAMES,
                "latency_class": "slow" if name == "parallel_research" else "normal",
            }
        )
    if name in _PLUGIN_LOCAL_TOOL_NAMES:
        metadata.update(
            {
                "intent_tags": ["plugin_question", "plugin_local"],
                "evidence_kind": "plugin_knowledge",
                "latency_class": "fast",
            }
        )
    if name in _PLUGIN_WEB_TOOL_NAMES:
        tags = _coerce_tags(metadata.get("intent_tags"))
        tags.update({"lookup", "plugin_latest"})
        metadata.update(
            {
                "intent_tags": sorted(tags),
                "evidence_kind": "web",
                "requires_network": True,
            }
        )
    if name in _NETWORK_TOOL_NAMES:
        tags = _coerce_tags(metadata.get("intent_tags"))
        tags.add("lookup")
        metadata.update(
            {
                "intent_tags": sorted(tags),
                "evidence_kind": "web",
                "requires_network": True,
                "latency_class": "slow" if name == "parallel_research" else metadata.get("latency_class", "normal"),
            }
        )
    if name in _MEMORY_TOOL_NAMES:
        tags = _coerce_tags(metadata.get("intent_tags"))
        tags.add("memory")
        metadata.update(
            {
                "intent_tags": sorted(tags),
                "evidence_kind": "memory",
                "latency_class": "fast",
            }
        )
    return metadata


def apply_tool_metadata_defaults(registry: ToolRegistry) -> None:
    for tool in registry.all():
        defaults = _default_tool_metadata(tool.name)
        metadata = dict(defaults)
        metadata.update(tool.metadata or {})
        tool.metadata = metadata


def tool_planner_metadata(tool: AgentTool) -> dict[str, Any]:
    metadata = dict(_default_tool_metadata(tool.name))
    metadata.update(tool.metadata or {})
    return {
        "name": tool.name,
        "description": tool.description,
        "intent_tags": sorted(_coerce_tags(metadata.get("intent_tags"))),
        "evidence_kind": str(metadata.get("evidence_kind", "generic") or "generic"),
        "requires_network": bool(metadata.get("requires_network", False)),
        "requires_image": bool(metadata.get("requires_image", False)),
        "latency_class": str(metadata.get("latency_class", "normal") or "normal"),
        "risk_level": str(metadata.get("risk_level", "low") or "low"),
        "local": bool(tool.local),
    }


def registry_planner_metadata(registry: ToolRegistry, *, active_only: bool = True) -> list[dict[str, Any]]:
    tools = registry.active() if active_only else registry.all()
    return [tool_planner_metadata(tool) for tool in tools]


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


def _tool_metadata_for_name(registry: ToolRegistry, name: str) -> dict[str, Any]:
    tool = registry.get(name)
    if tool is None:
        return _default_tool_metadata(name)
    return tool_planner_metadata(tool)


def _tool_tags(registry: ToolRegistry, name: str) -> set[str]:
    return _coerce_tags(_tool_metadata_for_name(registry, name).get("intent_tags"))


def _tool_is_admin(registry: ToolRegistry, name: str) -> bool:
    metadata = _tool_metadata_for_name(registry, name)
    return str(metadata.get("risk_level", "") or "").strip() == "admin" or name in _ADMIN_TOOL_NAMES


def _tool_requires_image(registry: ToolRegistry, name: str) -> bool:
    metadata = _tool_metadata_for_name(registry, name)
    return bool(metadata.get("requires_image", False)) or name in _IMAGE_REQUIRED_TOOL_NAMES


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
        # 闲聊也可能遇到看不懂的梗/专有名词/外号/分享内容，放行轻量查证工具，
        # 让模型"想查就能查"（不再无图就 return []）；有图时再叠加视觉类工具。
        result_schemas = [
            schema
            for schema in schemas
            if (
                schema_tool_name(schema) in _LIGHTWEIGHT_LOOKUP_TOOL_NAMES
                or (has_images and _tool_requires_image(registry, schema_tool_name(schema)))
            )
        ]
    elif effective_chat_intent == "image_generation":
        result_schemas = [
            schema
            for schema in schemas
            if (
                "image_generation" in _tool_tags(registry, schema_tool_name(schema))
                or schema_tool_name(schema) in _IMAGE_GENERATION_TOOL_NAMES
                or schema_tool_name(schema) in _IMAGE_GENERATION_CONTEXT_TOOL_NAMES
                or (has_images and _tool_requires_image(registry, schema_tool_name(schema)))
            )
        ]
    elif effective_chat_intent == "plugin_question":
        include_latest = str(plugin_question_intent or "").strip() == "latest"
        if has_images:
            include_latest = True
        result_schemas = [
            schema
            for schema in schemas
            if (
                "plugin_local" in _tool_tags(registry, schema_tool_name(schema))
                or schema_tool_name(schema) in _PLUGIN_LOCAL_TOOL_NAMES
                or (
                    include_latest
                    and (
                        "plugin_latest" in _tool_tags(registry, schema_tool_name(schema))
                        or schema_tool_name(schema) in _PLUGIN_WEB_TOOL_NAMES
                    )
                )
                or (has_images and _tool_requires_image(registry, schema_tool_name(schema)))
            )
        ]
    elif has_images:
        result_schemas = list(schemas)
    else:
        result_schemas = [
            schema
            for schema in schemas
            if not _tool_requires_image(registry, schema_tool_name(schema))
        ]
    return [
        schema
        for schema in result_schemas
        if not _tool_is_admin(registry, schema_tool_name(schema))
    ]


def semantic_tool_guidance() -> str:
    return (
        "工具使用总原则：能直接回答就别起工具；不确定、高风险、时效性强、明显需要查证时再调用工具。"
        "当当前消息包含你不认识、无法确定指代或可能有圈内含义的专有名词、角色名、作品名、游戏/动漫/卡牌术语、"
        "怪物/装备/地图名、外号、别称、缩写、谐音、空耳、梗或活动名时，如果可用工具里有 web_search、search_web、"
        "wiki_lookup、resolve_acg_entity 或 parallel_research，必须先调用合适工具查证；不要凭记忆猜，也不要直接在群里问"
        "“这是什么梗/哪个游戏/什么意思”。"
        "插件技术问题优先本地插件知识和源码工具。"
        "用户明确要求生成图片时，必须调用 generate_image，不要只给提示词。"
        "涉及本地天气、出行、城市或附近状态时，如果用户没明说地点，先看已注入的用户档案；仍不确定可调用记忆工具确认，不能猜城市。"
        "最终回复只输出纯文本，不要 markdown、项目符号列表、编号列表，也不要说正在查询、根据搜索结果或我需要确认一下。"
        "群聊接梗场景优先像群友接话，不要为了显得聪明而滥用工具。"
    )


__all__ = [
    "DEFAULT_AGENT_MAX_STEPS",
    "MAX_AGENT_MAX_STEPS",
    "MIN_AGENT_MAX_STEPS",
    "apply_tool_metadata_defaults",
    "normalize_agent_max_steps",
    "registry_planner_metadata",
    "schema_tool_name",
    "select_tool_schemas",
    "semantic_tool_guidance",
    "tool_planner_metadata",
]
