from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...core.evidence_envelope import EvidenceEnvelope
from ...core.qq_expression_tools import expression_tool_result_queued
from ..tool_registry import ToolRegistry
from .image_generation import _IMAGE_GENERATION_TOOL_NAME
from .tool_catalog import tool_runtime_metadata
from .wrappers import _is_direct_media_tool_result


@dataclass(frozen=True)
class DirectToolResult:
    text: str
    direct_output: bool = False
    bypass_length_limits: bool = False
    reason: str = ""
    failure_code: str = ""
    suppress_reply_recovery: bool = False
    evidence_envelope: dict[str, Any] | None = None


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
    queued_success = expression_tool_result_queued(text)
    if (
        side_effect == "message_recall"
        and final_behavior == "silence_on_success"
        and queued_success
    ):
        return DirectToolResult(
            text="[SILENCE]",
            reason="queued_message_recall",
            suppress_reply_recovery=True,
        )
    if (
        side_effect == "send_message"
        and final_behavior == "silence_on_success"
        and queued_success
    ):
        return DirectToolResult(text="[SILENCE]", reason="queued_send_message")
    if side_effect == "none" and final_behavior == "constrained_persona_output":
        envelope = EvidenceEnvelope.from_value(text)
        if envelope is None:
            return DirectToolResult(
                text="[SILENCE]",
                reason="invalid_evidence_envelope",
                suppress_reply_recovery=True,
            )
        return DirectToolResult(
            text=envelope.natural_fallback,
            direct_output=False,
            bypass_length_limits=False,
            reason="constrained_persona_output",
            evidence_envelope=envelope.to_dict(),
        )
    if _is_direct_media_tool_result(normalized_tool_name, text):
        return DirectToolResult(text=text, bypass_length_limits=True, reason="direct_media")
    if normalized_tool_name == _IMAGE_GENERATION_TOOL_NAME:
        return DirectToolResult(
            text="[NO_REPLY]",
            bypass_length_limits=False,
            reason="image_generation_result",
            failure_code="agent_image_generation_failed",
        )
    return None


__all__ = [
    "DirectToolResult",
    "direct_tool_result_from_contract",
    "metadata_tags",
    "recommended_tools_for_chat_intent",
]
