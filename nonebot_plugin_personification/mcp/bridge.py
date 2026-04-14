from __future__ import annotations

from typing import Any

from ..skill_runtime.mcp_compat import call_registered_mcp_tool, get_registered_mcp_endpoint


class McpBridge:
    async def call_remote(self, tool_name: str, args: dict[str, Any]) -> str:
        endpoint = get_registered_mcp_endpoint(tool_name)
        if endpoint is None:
            raise NotImplementedError(
                f"Remote MCP tool '{tool_name}' not configured. "
                "Declare it in a skillpack mcp block before calling."
            )
        return await call_registered_mcp_tool(tool_name, args)
