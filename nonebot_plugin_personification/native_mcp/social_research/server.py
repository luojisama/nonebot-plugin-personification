from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from ...core.mcp_builtin import builtin_social_tools
from .models import json_size_guard
from .service import SocialResearchService


_SAFE_RUNTIME_ERRORS = {
    "builtin MCP is disabled",
    "chromium_unavailable",
    "login_required",
    "manual_verification_required",
    "platform_disabled",
    "platform_request_failed",
    "playwright_unavailable",
    "risk_controlled",
    "interactive_auth_unavailable",
    "interactive_frame_unavailable",
    "interactive_page_outside_platform",
    "interactive_page_unavailable",
}


class SocialResearchMcpServer:
    def __init__(self) -> None:
        self.service = SocialResearchService()

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            return {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "personification-social-research", "version": "1.0.0"},
            }
        if method == "tools/list":
            return {"tools": builtin_social_tools()}
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            if name == "social_content_search":
                value = await self.service.search(arguments)
            elif name == "social_content_read":
                value = await self.service.read(arguments)
            elif name == "research_game_slang":
                value = await self.service.research(arguments)
            else:
                raise KeyError("tool_not_found")
            return {
                "content": [{"type": "text", "text": json_size_guard(value)}],
                "structuredContent": value,
                "isError": False,
            }
        if method == "personification/builtin/status":
            return await self.service.status()
        if method == "personification/builtin/configure":
            return await self.service.configure(params)
        if method == "personification/builtin/health":
            return await self.service.health()
        if method == "personification/builtin/auth/start":
            return await self.service.auth_start(params)
        if method == "personification/builtin/auth/status":
            return await self.service.auth_status(params)
        if method == "personification/builtin/auth/qrcode":
            return await self.service.auth_qrcode(params)
        if method == "personification/builtin/auth/frame":
            return await self.service.auth_frame(params)
        if method == "personification/builtin/auth/input":
            return await self.service.auth_input(params)
        if method == "personification/builtin/auth/finish":
            return await self.service.auth_finish(params)
        if method == "personification/builtin/auth/cancel":
            return await self.service.auth_cancel(params)
        if method == "personification/builtin/auth/logout":
            return await self.service.auth_logout(params)
        if method == "personification/builtin/cover/resolve":
            return self.service.cover_resolve(params)
        raise KeyError("method_not_found")

    async def close(self) -> None:
        await self.service.close()


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def main() -> None:
    server = SocialResearchMcpServer()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            request_id = message.get("id")
            method = str(message.get("method") or "")
            if request_id is None:
                continue
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            try:
                result = await server.dispatch(method, params)
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
            except KeyError as exc:
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": str(exc.args[0])[:100]}})
            except ValueError as exc:
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": str(exc)[:200]}})
            except TimeoutError:
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": "platform_timeout"}})
            except Exception as exc:
                raw_code = str(exc) if isinstance(exc, RuntimeError) else "operation_failed"
                code = raw_code if raw_code in _SAFE_RUNTIME_ERRORS else "operation_failed"
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": code[:100]}})
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
