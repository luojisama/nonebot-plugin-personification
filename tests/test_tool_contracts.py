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


def test_direct_tool_result_contract_formats_image_generation_failure() -> None:
    result = tool_contracts.direct_tool_result_from_contract(
        registry=tool_registry.ToolRegistry(),
        tool_name="generate_image",
        result_text="图片生成失败：empty image response",
    )

    assert result is not None
    assert result.text == "图片生成失败：图片服务没有返回图片数据"
    assert result.reason == "image_generation_result"
    assert result.bypass_length_limits is False


def test_recommended_tools_for_expression_uses_metadata_tags() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "send_custom_expression", {"intent_tags": ["expression"]})
    _register(registry, "search_web", {"intent_tags": ["lookup"]})

    assert tool_contracts.recommended_tools_for_chat_intent(registry, "expression") == [
        "send_custom_expression"
    ]
    assert tool_contracts.recommended_tools_for_chat_intent(registry, "banter") == []
