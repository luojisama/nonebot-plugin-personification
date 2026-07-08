from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.qq_expression_tools import expression_tool_result_queued
from ..tool_registry import ToolRegistry
from .image_generation import _IMAGE_GENERATION_TOOL_NAME
from .tool_catalog import tool_runtime_metadata
from .wrappers import _format_image_generation_failure, _is_direct_media_tool_result


@dataclass(frozen=True)
class DirectToolResult:
    text: str
    direct_output: bool = False
    bypass_length_limits: bool = False
    reason: str = ""


def metadata_tags(metadata: dict[str, Any]) -> set[str]:
    value = metadata.get("intent_tags", [])
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item or "").strip() for item in value if str(item or "").strip()}
    return set()


def recommended_tools_for_chat_intent(registry: ToolRegistry, chat_intent: str) -> list[str]:
    intent = str(chat_intent or "").strip()
    if intent == "image_generation":
        return ["generate_image"] if registry.get("generate_image") is not None else []
    if intent != "expression":
        return []
    names: list[str] = []
    for tool in registry.active():
        metadata = tool_runtime_metadata(registry, tool.name)
        if "expression" in metadata_tags(metadata):
            names.append(tool.name)
    return names[:6]


def direct_tool_result_from_contract(
    *,
    registry: ToolRegistry | None,
    tool_name: str,
    result_text: Any,
) -> DirectToolResult | None:
    text = str(result_text or "").strip()
    normalized_tool_name = str(tool_name or "").strip()
    metadata = tool_runtime_metadata(registry, normalized_tool_name)
    final_behavior = str(metadata.get("final_behavior", "") or "").strip()
    side_effect = str(metadata.get("side_effect", "") or "").strip()
    if (
        side_effect == "send_message"
        and final_behavior == "silence_on_success"
        and expression_tool_result_queued(text)
    ):
        return DirectToolResult(text="[SILENCE]", reason="queued_send_message")
    if _is_direct_media_tool_result(normalized_tool_name, text):
        return DirectToolResult(text=text, bypass_length_limits=True, reason="direct_media")
    if normalized_tool_name == _IMAGE_GENERATION_TOOL_NAME:
        return DirectToolResult(
            text=_format_image_generation_failure(text),
            bypass_length_limits=False,
            reason="image_generation_result",
        )
    return None


__all__ = [
    "DirectToolResult",
    "direct_tool_result_from_contract",
    "metadata_tags",
    "recommended_tools_for_chat_intent",
]
