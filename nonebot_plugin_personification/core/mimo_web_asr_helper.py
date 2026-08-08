from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .mimo_web_asr_runtime import MiMoWebAsrRuntime


_SAFE_ERRORS = {
    "auth_session_not_found",
    "chromium_unavailable",
    "interactive_auth_unavailable",
    "interactive_frame_unavailable",
    "interactive_page_outside_platform",
    "interactive_page_unavailable",
    "playwright_unavailable",
    "mimo_web_asr_dom_changed",
    "mimo_web_asr_generation_timeout",
    "mimo_web_asr_login_required",
    "mimo_web_asr_manual_verification_required",
    "mimo_web_asr_media_token_invalid",
    "mimo_web_asr_model_unavailable",
    "mimo_web_asr_network_risk_detected",
    "mimo_web_asr_network_risk_cooldown",
    "mimo_web_asr_local_rate_limited",
    "mimo_web_asr_upload_rejected",
}


class MiMoWebAsrHelperServer:
    def __init__(self) -> None:
        raw_root = str(os.environ.get("PERSONIFICATION_MIMO_WEB_ASR_ROOT") or "").strip()
        if not raw_root:
            raise RuntimeError("mimo_web_asr_root_missing")
        self.runtime = MiMoWebAsrRuntime(Path(raw_root).resolve())

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "personification-mimo-web-asr", "version": "1.0.0"},
            }
        if method == "tools/list":
            return {"tools": []}
        prefix = "personification/mimo-web-asr/"
        if not method.startswith(prefix):
            raise KeyError("method_not_found")
        action = method[len(prefix) :]
        if action == "configure":
            return self.runtime.configure(params)
        if action == "status":
            return self.runtime.status()
        if action == "probe":
            return await self.runtime.probe()
        if action == "analyze":
            return await self.runtime.analyze(params)
        if action == "auth/start":
            return await self.runtime.auth_start(str(params.get("owner") or ""))
        if action == "auth/status":
            return self.runtime.auth_status(str(params.get("session_id") or ""), str(params.get("owner") or ""))
        if action == "auth/frame":
            return await self.runtime.auth_frame(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
                after_revision=int(params.get("after_revision") or 0),
            )
        if action == "auth/input":
            request_action = params.get("action") if isinstance(params.get("action"), dict) else {}
            return await self.runtime.auth_input(
                str(params.get("session_id") or ""),
                str(params.get("owner") or ""),
                request_action,
            )
        if action == "auth/finish":
            return await self.runtime.auth_finish(str(params.get("session_id") or ""), str(params.get("owner") or ""))
        if action == "auth/cancel":
            return await self.runtime.auth_cancel(str(params.get("session_id") or ""), str(params.get("owner") or ""))
        if action == "logout":
            return await self.runtime.logout()
        raise KeyError("method_not_found")

    async def close(self) -> None:
        await self.runtime.close()


def _write(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def main() -> None:
    server = MiMoWebAsrHelperServer()
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
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": str(exc.args[0])[:100]}})
            except ValueError as exc:
                raw = str(exc or "")
                code = raw if raw in _SAFE_ERRORS else "mimo_web_asr_request_invalid"
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": code}})
            except Exception as exc:
                raw = str(exc or "") if isinstance(exc, RuntimeError) else ""
                code = raw if raw in _SAFE_ERRORS else "mimo_web_asr_process_failed"
                _write({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": code}})
    finally:
        await server.close()


if __name__ == "__main__":
    asyncio.run(main())
