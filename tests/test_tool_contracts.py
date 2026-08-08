from __future__ import annotations

import json

from ._loader import load_personification_module

tool_contracts = load_personification_module("plugin.personification.agent.runtime.tool_contracts")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")


async def _noop_handler(**_kwargs):  # noqa: ANN001
    return "ok"


def _register(registry, name: str, metadata: dict | None = None) -> None:  # noqa: ANN001
    registry.register(
        tool_registry.AgentTool(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_noop_handler,
            metadata=metadata or {},
        )
    )


def test_direct_tool_result_contract_silences_queued_send_action() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "send_custom_expression",
        {
            "side_effect": "send_message",
            "final_behavior": "silence_on_success",
            "intent_tags": ["expression"],
        },
    )

    result = tool_contracts.direct_tool_result_from_contract(
        registry=registry,
        tool_name="send_custom_expression",
        result_text=json.dumps({"ok": True, "queued": True}, ensure_ascii=False),
    )

    assert result is not None
    assert result.text == "[SILENCE]"
    assert result.reason == "queued_send_message"
    assert result.bypass_length_limits is False
    assert result.suppress_reply_recovery is False


def test_direct_tool_result_contract_silences_queued_message_recall_without_recovery() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "recall_latest_own_output",
        {
            "side_effect": "message_recall",
            "final_behavior": "silence_on_success",
            "ack_behavior": "suppress",
            "intent_tags": ["conversation_action"],
        },
    )

    result = tool_contracts.direct_tool_result_from_contract(
        registry=registry,
        tool_name="recall_latest_own_output",
        result_text=json.dumps({"ok": True, "queued": True}, ensure_ascii=False),
    )

    assert result is not None
    assert result.text == "[SILENCE]"
    assert result.reason == "queued_message_recall"
    assert result.suppress_reply_recovery is True


def test_direct_tool_result_contract_keeps_media_marker_direct() -> None:
    result = tool_contracts.direct_tool_result_from_contract(
        registry=tool_registry.ToolRegistry(),
        tool_name="generate_image",
        result_text="[IMAGE_B64]QUJD[/IMAGE_B64]",
    )

    assert result is not None
    assert result.text == "[IMAGE_B64]QUJD[/IMAGE_B64]"
    assert result.reason == "direct_media"
    assert result.bypass_length_limits is True


def test_direct_tool_result_contract_marks_image_generation_failure_silent() -> None:
    result = tool_contracts.direct_tool_result_from_contract(
        registry=tool_registry.ToolRegistry(),
        tool_name="generate_image",
        result_text="图片生成失败：empty image response",
    )

    assert result is not None
    assert result.text == "[NO_REPLY]"
    assert result.reason == "image_generation_result"
    assert result.failure_code == "agent_image_generation_failed"
    assert result.bypass_length_limits is False


def test_avatar_pair_contract_uses_constrained_persona_output() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "inspect_group_user_avatar_pair",
        {"side_effect": "none", "final_behavior": "constrained_persona_output"},
    )
    result = tool_contracts.direct_tool_result_from_contract(
        registry=registry,
        tool_name="inspect_group_user_avatar_pair",
        result_text=json.dumps(
            {
                "type": "personification_evidence_envelope",
                "available": True,
                "allowed_claims": ["两张头像在构图、元素或风格上呈现视觉配套。"],
                "forbidden_inferences": ["不能据此判断两位用户现实中是情侣。"],
                "confidence": 0.9,
                "natural_fallback": "两张头像在构图、元素或风格上呈现视觉配套。",
            },
            ensure_ascii=False,
        ),
    )

    assert result is not None
    assert result.direct_output is False
    assert result.bypass_length_limits is False
    assert result.reason == "constrained_persona_output"
    assert "呈现视觉配套" in result.text
    assert result.evidence_envelope is not None
    assert result.evidence_envelope["forbidden_inferences"] == ["不能据此判断两位用户现实中是情侣。"]


def test_recommended_tools_for_expression_uses_metadata_tags() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "send_custom_expression", {"intent_tags": ["expression"]})
    _register(registry, "search_web", {"intent_tags": ["lookup"]})

    assert tool_contracts.recommended_tools_for_chat_intent(registry, "expression") == [
        "send_custom_expression"
    ]
    assert tool_contracts.recommended_tools_for_chat_intent(registry, "banter") == []
