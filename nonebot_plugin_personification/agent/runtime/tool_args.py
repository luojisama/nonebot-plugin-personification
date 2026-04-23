from __future__ import annotations

import json
from typing import Any

from ..query_rewriter import ContextualQueryRewrite
from ..tool_registry import ToolRegistry
from .constants import MAX_LOOKUP_QUERY_VARIANTS
from .intent import compact_lookup_query

_RETRYABLE_LOOKUP_TOOLS = frozenset(
    {"web_search", "search_web", "wiki_lookup", "resolve_acg_entity", "collect_resources", "search_images"}
)
def _parse_json_tool_result(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw or not raw.startswith("{"):
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def schema_allowed_parameters(registry: ToolRegistry, tool_name: str) -> set[str]:
    tool = registry.get(tool_name)
    if tool is None:
        return set()
    params = tool.parameters if isinstance(tool.parameters, dict) else {}
    properties = params.get("properties", {}) if isinstance(params, dict) else {}
    if not isinstance(properties, dict):
        return set()
    return {str(key or "").strip() for key in properties.keys() if str(key or "").strip()}


def tool_allows_parameter(registry: ToolRegistry, tool_name: str, parameter_name: str) -> bool:
    allowed = schema_allowed_parameters(registry, tool_name)
    if not allowed:
        return False
    return str(parameter_name or "").strip() in allowed


def sanitize_tool_args_for_schema(
    *,
    registry: ToolRegistry,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any]:
    args = dict(tool_args or {})
    allowed = schema_allowed_parameters(registry, tool_name)
    if not allowed:
        return {}
    return {key: value for key, value in args.items() if key in allowed}


def query_variants_for_tool(
    *,
    tool_name: str,
    tool_args: dict[str, Any] | None = None,
    rewritten_query: ContextualQueryRewrite | None,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> list[str]:
    candidates: list[str] = []
    if rewritten_query is not None:
        for query in [rewritten_query.primary_query, *(rewritten_query.query_candidates or [])]:
            cleaned = compact_lookup_query(query)
            if cleaned and cleaned not in candidates:
                candidates.append(cleaned)
    provided_query = compact_lookup_query(str((tool_args or {}).get("query", "") or ""))
    if provided_query and provided_query not in candidates:
        candidates.append(provided_query)
    if previous_tool_name and previous_tool_result_text:
        refined = compact_lookup_query(previous_tool_result_text)
        if refined and refined not in candidates:
            candidates.append(refined)
    if tool_name not in _RETRYABLE_LOOKUP_TOOLS:
        return candidates[:1]
    return candidates[:MAX_LOOKUP_QUERY_VARIANTS]


def rewrite_tool_args(
    *,
    registry: ToolRegistry | None = None,
    tool_name: str,
    tool_args: dict[str, Any],
    rewritten_query: ContextualQueryRewrite | None,
    user_images: list[str] | None = None,
    previous_tool_name: str = "",
    previous_tool_result_text: str = "",
) -> dict[str, Any]:
    if registry is None:
        args = dict(tool_args or {})
    else:
        args = sanitize_tool_args_for_schema(
            registry=registry,
            tool_name=tool_name,
            tool_args=tool_args,
        )
    user_images = list(user_images or [])

    allows_query = registry is None or tool_allows_parameter(registry, tool_name, "query")
    if allows_query:
        current_query = compact_lookup_query(str(args.get("query", "") or ""))
        if not current_query:
            variants = query_variants_for_tool(
                tool_name=tool_name,
                tool_args=args,
                rewritten_query=rewritten_query,
                previous_tool_name=previous_tool_name,
                previous_tool_result_text=previous_tool_result_text,
            )
            if variants:
                current_query = variants[0]
        if current_query:
            args["query"] = current_query

    if user_images:
        for key in ("images", "image_urls"):
            if (registry is None or tool_allows_parameter(registry, tool_name, key)) and key not in args:
                args[key] = list(user_images)
        if (registry is None or tool_allows_parameter(registry, tool_name, "image_index")) and "image_index" not in args:
            args["image_index"] = 1
    if tool_name == "resolve_acg_entity":
        if user_images and (registry is None or tool_allows_parameter(registry, tool_name, "image_context")):
            args.setdefault("image_context", True)
        if previous_tool_name == "vision_analyze" and (
            registry is None or tool_allows_parameter(registry, tool_name, "visual_hints")
        ):
            visual_hints = _parse_json_tool_result(previous_tool_result_text)
            if isinstance(visual_hints, dict):
                args.setdefault("visual_hints", visual_hints)

    if registry is None:
        return args
    return sanitize_tool_args_for_schema(registry=registry, tool_name=tool_name, tool_args=args)


_schema_allowed_parameters = schema_allowed_parameters
_tool_allows_parameter = tool_allows_parameter
_sanitize_tool_args_for_schema = sanitize_tool_args_for_schema
_query_variants_for_tool = query_variants_for_tool
_rewrite_tool_args = rewrite_tool_args

__all__ = [
    "_query_variants_for_tool",
    "_rewrite_tool_args",
    "_sanitize_tool_args_for_schema",
    "_schema_allowed_parameters",
    "_tool_allows_parameter",
    "query_variants_for_tool",
    "rewrite_tool_args",
    "sanitize_tool_args_for_schema",
    "schema_allowed_parameters",
    "tool_allows_parameter",
]
