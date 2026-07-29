from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from ...core import webui_audit_log
from ...core.operation_diagnostics import detail, diagnostic, step
from ...core.qq_recall import QQRecallService
from ..deps import AdminIdentity, require_admin


class RecallOperationBody(BaseModel):
    bot_id: str = Field(..., min_length=1, max_length=32)
    conversation_kind: Literal["group", "private"]
    conversation_id: str = Field(..., min_length=1, max_length=32)
    confirmation: str = Field(..., min_length=1, max_length=240)


def _bundle(runtime: Any) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None or getattr(bundle, "qq_outbound_ledger", None) is None:
        raise HTTPException(status_code=503, detail="QQ outbound ledger 未就绪")
    return bundle


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _get_bots(runtime: Any, bundle: Any) -> dict[str, Any]:
    for holder in (runtime, bundle):
        getter = getattr(holder, "get_bots", None)
        if not callable(getter):
            continue
        try:
            bots = getter() or {}
        except Exception:
            continue
        if isinstance(bots, dict):
            return bots
    return {}


def _select_bot(runtime: Any, bundle: Any, bot_id: str) -> Any:
    target = str(bot_id or "").strip()
    for key, bot in _get_bots(runtime, bundle).items():
        actual = str(getattr(bot, "self_id", "") or key or "").strip()
        if actual == target:
            return bot
    raise HTTPException(status_code=404, detail="指定 Bot 当前不在线")


def _recall_diagnostic(result: Any) -> dict[str, Any]:
    status = str(getattr(result, "status", "unknown") or "unknown")
    code = str(getattr(result, "code", "outcome_unknown") or "outcome_unknown")
    total = int(getattr(result, "total_count", 0) or 0)
    recalled = int(getattr(result, "recalled_count", 0) or 0)
    operation_id = str(getattr(result, "outbound_operation_id", "") or "")
    ok = status == "succeeded"
    if ok:
        title = "Bot 消息撤回完成"
        message = f"该 operation 的 {recalled} 条平台消息均已确认撤回。"
        step_status = "ok"
    elif status == "unknown":
        title = "撤回结果未知"
        message = "协议端未返回可确认结果；该 operation 已封存，禁止自动重试。"
        step_status = "unknown"
    elif status == "partial":
        title = "Bot 消息仅部分撤回"
        message = f"已确认撤回 {recalled}/{total} 条；剩余部分不会自动重试。"
        step_status = "warn"
    elif status == "definite_failure":
        title = "Bot 消息撤回失败"
        message = "协议端已明确拒绝撤回；该 operation 不会自动重试。"
        step_status = "error"
    else:
        title = "没有可撤回的完整 operation"
        message = "目标可能超出窗口、不完整、已撤回、已尝试，或不属于提交的会话范围。"
        step_status = "error"
    return diagnostic(
        ok=ok,
        code=f"qq_recall_{code}",
        phase="operation_complete" if ok else "recall_dispatch",
        title=title,
        message=message,
        details=(
            detail("operation_id", operation_id or "-", "ok" if operation_id else "warn"),
            detail("撤回条数", f"{recalled}/{total}", "ok" if ok else "warn"),
            detail("saga status", status, "ok" if ok else "warn"),
        ),
        steps=(
            step("scope_validation", "服务端重新验证 Bot 与会话范围", "ok"),
            step("operation_claim", "原子 claim 完整 operation", "ok" if status != "no_candidate" else "error"),
            step("recall_dispatch", "逐条调用 OneBot delete_msg", step_status, message),
        ),
        suggestion=(
            "无需重复提交。"
            if ok
            else "先在 QQ 客户端核对实际状态；unknown/partial operation 不得重试。"
        ),
        retryable=False,
        partial=status == "partial",
        outcome_unknown=status == "unknown",
        operation_id=operation_id,
    )


def build_outbound_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/outbound", tags=["outbound"])

    @router.get("/recent")
    async def recent(
        response: Response,
        bot_id: str = Query(default="", max_length=32),
        conversation_kind: str = Query(default="", max_length=16),
        conversation_id: str = Query(default="", max_length=32),
        status: str = Query(default="", max_length=16),
        recalled: bool | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        ledger = _bundle(runtime).qq_outbound_ledger
        try:
            rows = await asyncio.to_thread(
                ledger.list_recent,
                bot_id=bot_id,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
                status=status,
                recalled=recalled,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="出站账本筛选参数无效") from exc
        return {"messages": rows, "available": True}

    @router.post("/{operation_id}/recall")
    async def recall_operation(
        operation_id: str,
        body: RecallOperationBody,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _no_store(response)
        normalized_operation_id = str(operation_id or "").strip()
        if not normalized_operation_id or len(normalized_operation_id) > 200:
            raise HTTPException(status_code=400, detail="operation_id 无效")
        if not body.conversation_id.isdigit() or int(body.conversation_id) <= 0:
            raise HTTPException(status_code=400, detail="conversation_id 无效")
        expected_confirmation = f"RECALL {normalized_operation_id}"
        if body.confirmation != expected_confirmation:
            webui_audit_log.record(
                action="qq_outbound_recall",
                qq=admin.qq,
                device_id=admin.device_id,
                target=normalized_operation_id,
                detail={
                    "bot_id": body.bot_id,
                    "conversation_kind": body.conversation_kind,
                    "conversation_id": body.conversation_id,
                    "code": "confirmation_mismatch",
                },
                outcome="denied",
            )
            raise HTTPException(
                status_code=400,
                detail=f"确认串必须精确等于 {expected_confirmation}",
            )
        bundle = _bundle(runtime)
        try:
            bot = _select_bot(runtime, bundle, body.bot_id)
        except HTTPException:
            webui_audit_log.record(
                action="qq_outbound_recall",
                qq=admin.qq,
                device_id=admin.device_id,
                target=normalized_operation_id,
                detail={
                    "bot_id": body.bot_id,
                    "conversation_kind": body.conversation_kind,
                    "conversation_id": body.conversation_id,
                    "code": "bot_unavailable",
                },
                outcome="denied",
            )
            raise
        service = QQRecallService(
            bundle.qq_outbound_ledger,
            plugin_config=getattr(runtime, "plugin_config", None),
            logger=getattr(runtime, "logger", None),
        )
        result = await service.recall_operation(
            bot=bot,
            operation_id=normalized_operation_id,
            conversation_kind=body.conversation_kind,
            conversation_id=body.conversation_id,
            requester_user_id=admin.qq,
            cutoff=time.time(),
        )
        result_diagnostic = _recall_diagnostic(result)
        webui_audit_log.record(
            action="qq_outbound_recall",
            qq=admin.qq,
            device_id=admin.device_id,
            target=normalized_operation_id,
            detail={
                "bot_id": body.bot_id,
                "conversation_kind": body.conversation_kind,
                "conversation_id": body.conversation_id,
                "status": result.status,
                "code": result.code,
                "total_count": result.total_count,
                "recalled_count": result.recalled_count,
            },
            outcome="ok" if result.ok else result.status,
        )
        return {
            "status": result.status,
            "code": result.code,
            "outbound_operation_id": result.outbound_operation_id,
            "total_count": result.total_count,
            "recalled_count": result.recalled_count,
            "diagnostic": result_diagnostic,
        }

    return router


__all__ = ["build_outbound_router"]
