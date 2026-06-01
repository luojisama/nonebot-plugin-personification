from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps import AdminIdentity, require_admin


def _tool_caller(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    # 优先 reply_processor_deps.runtime.agent_tool_caller，其次 bundle.agent_tool_caller
    deps = getattr(bundle, "reply_processor_deps", None)
    if deps is not None:
        runtime_inner = getattr(deps, "runtime", None)
        if runtime_inner is not None:
            caller = getattr(runtime_inner, "agent_tool_caller", None)
            if caller is not None:
                return caller
    return getattr(bundle, "agent_tool_caller", None)


def build_test_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/test", tags=["test"])

    @router.post("/chat")
    async def test_chat(
        body: dict = Body(default_factory=dict),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        prompt = str(body.get("prompt", "") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")
        system = str(body.get("system", "") or "你是测试助手，简洁回复。")
        use_builtin_search = bool(body.get("use_builtin_search", False))
        caller = _tool_caller(runtime)
        if caller is None:
            raise HTTPException(status_code=503, detail="tool_caller 未就绪（agent 流水线未启用？）")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        started = time.time()
        try:
            response = await caller.chat_with_tools(
                messages=messages,
                tools=[],
                use_builtin_search=use_builtin_search,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"调用失败：{exc}") from exc
        duration_ms = int((time.time() - started) * 1000)
        content = str(getattr(response, "content", "") or "")
        finish_reason = str(getattr(response, "finish_reason", "") or "")
        usage_obj = getattr(response, "usage", None) or {}
        if not isinstance(usage_obj, dict):
            usage_obj = {
                "prompt_tokens": getattr(usage_obj, "prompt_tokens", 0),
                "completion_tokens": getattr(usage_obj, "completion_tokens", 0),
                "total_tokens": getattr(usage_obj, "total_tokens", 0),
            }
        return {
            "content": content,
            "finish_reason": finish_reason,
            "duration_ms": duration_ms,
            "usage": {
                "prompt_tokens": int(usage_obj.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage_obj.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage_obj.get("total_tokens", 0) or 0),
            },
            "model_used": str(getattr(response, "model_used", "") or ""),
        }

    return router
