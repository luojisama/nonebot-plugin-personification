from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..deps import AdminIdentity, require_admin

_TEST_ALL_TIMEOUT_SECONDS = 40
_PROMPT_PREVIEW_MAX_BYTES = 256 * 1024


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

    @router.post("/chat-all")
    async def test_chat_all(
        body: dict = Body(default_factory=dict),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """向当前配置的每一个 provider 各发一次测试，返回各自内容与延迟。"""
        prompt = str(body.get("prompt", "") or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt 不能为空")
        system = str(body.get("system", "") or "你是测试助手，简洁回复。")
        plugin_config = getattr(runtime, "plugin_config", None)
        logger = getattr(runtime, "logger", None)
        if plugin_config is None:
            raise HTTPException(status_code=503, detail="运行时未就绪")

        from ...core import ai_routes
        from ...core.safety_filter import detect_api_block

        providers = ai_routes.list_primary_providers(plugin_config, logger)
        if not providers:
            raise HTTPException(status_code=400, detail="未配置任何 provider（api_pools 为空）")

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]

        async def _probe(index: int, provider: dict) -> dict:
            name = str(provider.get("name", "") or "") or f"{provider.get('api_type', '')}:{provider.get('model', '')}"
            base = {
                "index": index,
                "name": name,
                "api_type": str(provider.get("api_type", "") or ""),
                "model": str(provider.get("model", "") or ""),
                "priority": int(provider.get("priority", index) or 0),
            }
            started = time.time()
            try:
                caller = ai_routes.build_single_provider_caller(plugin_config, provider)
                response = await asyncio.wait_for(
                    caller.chat_with_tools(messages=messages, tools=[], use_builtin_search=False),
                    timeout=_TEST_ALL_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                base.update(ok=False, duration_ms=int((time.time() - started) * 1000),
                            error=f"超时（>{_TEST_ALL_TIMEOUT_SECONDS}s）")
                return base
            except Exception as exc:
                base.update(ok=False, duration_ms=int((time.time() - started) * 1000), error=str(exc)[:300])
                return base
            duration_ms = int((time.time() - started) * 1000)
            content = str(getattr(response, "content", "") or "")
            blocked = detect_api_block(response)
            base.update(
                ok=not blocked,
                duration_ms=duration_ms,
                content=content[:2000],
                finish_reason=str(getattr(response, "finish_reason", "") or ""),
                blocked_reason=blocked,
                model_used=str(getattr(response, "model_used", "") or ""),
                error="" if not blocked else f"被安全策略拦截：{blocked}",
            )
            return base

        results = await asyncio.gather(*[_probe(i, p) for i, p in enumerate(providers)])
        return {"count": len(results), "results": list(results)}

    @router.get("/persona-prompt")
    async def persona_prompt_preview(
        path: str = Query(default=""),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """预览人设提示词文件内容。

        不传 path 时预览当前配置生效的人设文件（prompt_path / system_path /
        system_prompt 指向的文件）；传 path 时预览该指定路径（管理员操作，文本只读）。
        """
        plugin_config = getattr(runtime, "plugin_config", None)
        if plugin_config is None:
            raise HTTPException(status_code=503, detail="运行时未就绪")

        from ...core.prompt_loader import _resolve_candidate_path

        def _inline(text: str, src: str) -> dict:
            return {
                "exists": True,
                "is_file": False,
                "source": src,
                "resolved_path": "",
                "content": text,
                "size": len(text.encode("utf-8")),
            }

        def _safe_is_file(p: Any) -> bool:
            try:
                return p.is_file()
            except OSError:  # 文件名过长等 → 视为非文件
                return False

        requested = str(path or "").strip().strip('"').strip("'")
        source = "指定路径"
        inline_fallback = ""  # 若按路径解析不到文件，回退展示的内联文本
        if not requested:
            requested = str(
                getattr(plugin_config, "personification_prompt_path", "")
                or getattr(plugin_config, "personification_system_path", "")
                or ""
            ).strip().strip('"').strip("'")
            source = "prompt_path / system_path"
            if not requested:
                raw_sp = str(getattr(plugin_config, "personification_system_prompt", "") or "").strip()
                if not raw_sp:
                    raise HTTPException(status_code=404, detail="未配置人设文件路径，且 system_prompt 为空")
                # system_prompt 较短时可能是一个文件路径；解析不到文件则按内联文本展示
                if len(raw_sp) < 260:
                    requested = raw_sp.strip('"').strip("'")
                    source = "system_prompt（按路径解析）"
                    inline_fallback = raw_sp
                else:
                    return _inline(raw_sp, "system_prompt（内联文本）")

        try:
            resolved = _resolve_candidate_path(requested)
            is_file = _safe_is_file(resolved)
        except OSError:  # 路径过长/非法 → 当作"不是文件"
            resolved = None
            is_file = False
        if not is_file:
            if inline_fallback:
                return _inline(inline_fallback, "system_prompt（内联文本）")
            return {
                "exists": False,
                "is_file": False,
                "source": source,
                "resolved_path": str(resolved.absolute()) if resolved is not None else requested,
                "content": "",
                "size": 0,
            }
        try:
            size = resolved.stat().st_size
            if size > _PROMPT_PREVIEW_MAX_BYTES:
                raise HTTPException(status_code=413, detail=f"文件过大（{size} 字节），超过预览上限")
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取失败：{exc}") from exc
        return {
            "exists": True,
            "is_file": True,
            "source": source,
            "resolved_path": str(resolved.absolute()),
            "content": content,
            "size": size,
        }

    return router
