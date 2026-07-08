from __future__ import annotations

import asyncio

from ._loader import load_personification_module

tool_health = load_personification_module("plugin.personification.core.tool_health")
tool_registry_mod = load_personification_module("plugin.personification.agent.tool_registry")


def _tool(name: str, handler, metadata: dict | None = None):  # noqa: ANN001
    return tool_registry_mod.AgentTool(
        name=name,
        description=f"{name} tool",
        parameters={},
        handler=handler,
        metadata=metadata or {"requires_network": True, "side_effect": "none"},
    )


def test_probe_failure_temporarily_hides_network_tool_from_active_registry() -> None:
    async def failing_handler(**_kwargs):  # noqa: ANN001
        raise TimeoutError("connect timeout")

    tool_health.reset_tool_health_statuses()
    registry = tool_registry_mod.ToolRegistry()
    registry.register(_tool("wiki_lookup", failing_handler))

    result = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))

    assert result["checked"] == 1
    assert result["disabled"] == 1
    assert tool_health.is_tool_temporarily_disabled("wiki_lookup") is True
    assert [tool.name for tool in registry.active()] == []


def test_probe_success_restores_previously_hidden_tool() -> None:
    calls = {"count": 0}

    async def flaky_handler(**_kwargs):  # noqa: ANN001
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("connect timeout")
        return "正常结果"

    tool_health.reset_tool_health_statuses()
    registry = tool_registry_mod.ToolRegistry()
    registry.register(_tool("wiki_lookup", flaky_handler))

    first = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))
    second = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))

    assert first["disabled"] == 1
    assert second["restored"] == 1
    assert tool_health.is_tool_temporarily_disabled("wiki_lookup") is False
    assert [tool.name for tool in registry.active()] == ["wiki_lookup"]


def test_probe_accepts_structured_payload_with_empty_error_field() -> None:
    async def handler(**_kwargs):  # noqa: ANN001
        return '{"ok": true, "error": null, "results": [{"title": "ok"}]}'

    tool_health.reset_tool_health_statuses()
    registry = tool_registry_mod.ToolRegistry()
    registry.register(_tool("wiki_lookup", handler))

    result = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))

    assert result["checked"] == 1
    assert result["disabled"] == 0
    assert tool_health.is_tool_temporarily_disabled("wiki_lookup") is False


def test_web_search_probe_uses_only_supported_arguments() -> None:
    async def handler(query: str):  # noqa: ANN001
        return f"searched {query}"

    tool_health.reset_tool_health_statuses()
    registry = tool_registry_mod.ToolRegistry()
    registry.register(_tool("web_search", handler))

    result = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))

    assert result["checked"] == 1
    assert result["disabled"] == 0


def test_probe_skips_send_message_side_effect_tools() -> None:
    async def handler(**_kwargs):  # noqa: ANN001
        raise AssertionError("side-effect tools must not be probed")

    tool_health.reset_tool_health_statuses()
    registry = tool_registry_mod.ToolRegistry()
    registry.register(
        _tool(
            "search_and_send_images",
            handler,
            metadata={"requires_network": True, "side_effect": "send_message"},
        )
    )

    result = asyncio.run(tool_health.probe_registry_tools(registry=registry, timeout_seconds=1))

    assert result["checked"] == 0
    assert [tool.name for tool in registry.active()] == ["search_and_send_images"]
