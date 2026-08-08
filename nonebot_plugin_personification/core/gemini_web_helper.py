from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .gemini_web_runtime import GeminiWebRuntime


_SAFE_ERRORS = {
    "auth_session_not_found",
    "chromium_unavailable",
    "interactive_auth_unavailable",
    "interactive_frame_unavailable",
    "interactive_page_outside_platform",
    "interactive_page_unavailable",
    "playwright_unavailable",
    "gemini_web_dom_changed",
    "gemini_web_disabled",
    "gemini_web_generation_timeout",
    "gemini_web_login_required",
    "gemini_web_manual_verification_required",
    "gemini_web_media_kind_invalid",
    "gemini_web_media_token_invalid",
    "gemini_web_network_risk_detected",
    "gemini_web_network_risk_cooldown",
    "gemini_web_local_rate_limited",
    "gemini_web_upload_rejected",
}


class GeminiWebHelperServer:
    def __init__(self) -> None:
        root = Path(os.environ.get("PERSONIFICATION_GEMINI_WEB_ROOT") or "").resolve()
        if not str(os.environ.get("PERSONIFICATION_GEMINI_WEB_ROOT") or "").strip():
            raise RuntimeError("gemini_web_root_missing")
        self.runtime = GeminiWebRuntime(root)

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "personification-gemini-web", "version": "1.0.0"},
            }
        if method == "tools/list":
            return {"tools": []}
        if method == "personification/gemini-web/configure":
            return self.runtime.configure(params)
        if method == "personification/gemini-web/status":
            return self.runtime.status()
        if method == "personification/gemini-web/probe":
            return await self.runtime.probe()
        if method == "personification/gemini-web/analyze":
            return await self.runtime.analyze(params)
        if method == "personification/gemini-web/auth/start":
            return await self.runtime.auth_start(str(params.get("owner") or ""))
        if method == "personification/gemini-web/auth/status":
            return self.runtime.auth_status(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
            )
        if method == "personification/gemini-web/auth/frame":
            return await self.runtime.auth_frame(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
                after_revision=int(params.get("after_revision") or 0),
            )
        if method == "personification/gemini-web/auth/input":
            action = params.get("action") if isinstance(params.get("action"), dict) else {}
            return await self.runtime.auth_input(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
                action,
            )
        if method == "personification/gemini-web/auth/finish":
            return await self.runtime.auth_finish(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
            )
        if method == "personification/gemini-web/auth/cancel":
            return await self.runtime.auth_cancel(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
            )
        if method == "personification/gemini-web/logout":
            return await self.runtime.logout()
        raise KeyError("method_not_found")

    async def close(self) -> None:
        await self.runtime.close()


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def main() -> None:
    server = GeminiWebHelperServer()
    try:
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                return
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") is None:
                continue
            request_id = message.get("id")
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            try:
                result = await server.dispatch(method, params)
                _write({"jsonrpc": "2.0", "id": request_id, "result": result})
            except KeyError as exc:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": str(exc.args[0])[:100]},
                    }
                )
            except ValueError as exc:
                raw = str(exc or "")
                code = raw if raw in _SAFE_ERRORS else "gemini_web_request_invalid"
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": code}})
            except Exception as exc:
                raw = str(exc or "") if isinstance(exc, RuntimeError) else ""
                code = raw if raw in _SAFE_ERRORS else "gemini_web_process_failed"
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": code}})
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
