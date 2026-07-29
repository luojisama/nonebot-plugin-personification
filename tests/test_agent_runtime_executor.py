from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module


executor = load_personification_module("plugin.personification.agent.runtime.executor")
mcp_bridge = load_personification_module("plugin.personification.mcp.bridge")


def test_remote_tool_uses_top_level_mcp_bridge(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, dict[str, object]]] = []

    async def call_remote(_self, tool_name: str, args: dict[str, object]) -> str:
        calls.append((tool_name, args))
        return "remote-ok"

    async def unused_handler(**_kwargs) -> str:
        raise AssertionError("remote tools must use McpBridge")

    monkeypatch.setattr(mcp_bridge.McpBridge, "call_remote", call_remote)
    tool = SimpleNamespace(local=False, handler=unused_handler)

    result = asyncio.run(
        executor._invoke_tool_handler(
            tool_name="social_content_search",
            tool=tool,
            tool_args={"query": "花来"},
        )
    )

    assert result == "remote-ok"
    assert calls == [("social_content_search", {"query": "花来"})]


def test_local_tool_still_uses_registered_handler() -> None:
    calls: list[dict[str, object]] = []

    async def local_handler(**kwargs) -> str:  # noqa: ANN003
        calls.append(kwargs)
        return "local-ok"

    tool = SimpleNamespace(local=True, handler=local_handler)
    result = asyncio.run(
        executor._invoke_tool_handler(
            tool_name="local_lookup",
            tool=tool,
            tool_args={"query": "test"},
        )
    )

    assert result == "local-ok"
    assert calls == [{"query": "test"}]
