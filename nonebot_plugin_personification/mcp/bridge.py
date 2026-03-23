class McpBridge:
    async def call_remote(self, tool_name: str, args: dict) -> str:
        raise NotImplementedError(
            f"Remote MCP tool '{tool_name}' not configured. "
            "Set tool.local=True or configure mcp/bridge.py."
        )
