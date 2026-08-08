from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ...core import plugin_runtime_logs, reply_turn_trace, webui_audit_log
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ..deps import AdminIdentity, get_client_ip, require_admin


def build_log_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/logs", tags=["logs"])

    @router.get("/recent")
    async def recent(
        limit: int = Query(default=200, ge=1, le=500),
        level: str = Query(default=""),
        q: str = Query(default=""),
        cursor: int = Query(default=0, ge=0),
        trace_id: str = Query(default=""),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        cfg = getattr(runtime, "plugin_config", None)

        def _query() -> dict:
            pruned = plugin_runtime_logs.maybe_prune(config=cfg)
            page = plugin_runtime_logs.query_page(
                limit=limit,
                level=level,
                q=q,
                cursor=cursor,
                trace_id=trace_id,
            )
            page.update(
                {
                    "retention_days": plugin_runtime_logs.retention_days_from_config(cfg),
                    "pruned": pruned,
                    "writer": plugin_runtime_logs.writer_status(),
                }
            )
            return page

        return await run_in_threadpool(_query)

    @router.get("/trace/{trace_id}")
    async def trace_detail(
        trace_id: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        trace = reply_turn_trace.get_trace(trace_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="未找到该 trace")
        rows = await run_in_threadpool(plugin_runtime_logs.query_recent, limit=120, trace_id=trace_id)
        return {
            "trace": trace,
            "logs": rows,
            "process": reply_turn_trace.build_process_view(trace, logs=rows),
        }

    def _stage_detail(trace: dict, key: str) -> str:
        for stage in list(trace.get("stages") or []):
            if isinstance(stage, dict) and str(stage.get("key") or "") == key:
                return str(stage.get("detail") or "")[:500]
        return ""

    @router.get("/traces")
    async def traces(
        limit: int = Query(default=100, ge=1, le=200),
        session_type: str = Query(default=""),
        group_id: str = Query(default=""),
        user_id: str = Query(default=""),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        rows = reply_turn_trace.query_recent(
            limit=limit,
            session_type=session_type,
            group_id=group_id,
            user_id=user_id,
        )
        entries = []
        for trace in rows:
            process = reply_turn_trace.build_process_view(trace, logs=[])
            summary = process.get("summary") if isinstance(process, dict) else {}
            detail = trace.get("detail") if isinstance(trace.get("detail"), dict) else {}
            entries.append(
                {
                    "trace_id": trace.get("trace_id", ""),
                    "ts": trace.get("ts", 0),
                    "session_type": trace.get("session_type", ""),
                    "group_id": trace.get("group_id", ""),
                    "user_id": trace.get("user_id", ""),
                    "outcome": trace.get("outcome", ""),
                    "diagnosis_code": trace.get("diagnosis_code", ""),
                    "incoming_text": detail.get("incoming_text") or _stage_detail(trace, "incoming_message"),
                    "outgoing_text": detail.get("outgoing_text") or _stage_detail(trace, "outgoing_message"),
                    "stage_count": summary.get("stage_count", 0) if isinstance(summary, dict) else 0,
                    "warn_count": summary.get("warn_count", 0) if isinstance(summary, dict) else 0,
                    "error_count": summary.get("error_count", 0) if isinstance(summary, dict) else 0,
                }
            )
        return {"entries": entries}

    @router.delete("/clear")
    async def clear(
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            deleted = await run_in_threadpool(plugin_runtime_logs.clear_all)
        except Exception as exc:
            report = exception_diagnostic(
                exc,
                phase="persistence",
                title="插件日志清空未完成",
                message="服务器未能确认持久日志已清空。",
                suggestion="先刷新日志列表确认当前状态；仍有日志时再重试。",
                retryable=True,
            )
            report["code"] = "plugin_logs_clear_failed"
            report["steps"] = [
                step("persist", "清空持久日志", "unknown", "清理过程异常，最终状态需要重新读取确认。").to_dict(),
                step("audit", "记录管理员操作", "skipped", "日志清理结果未知。").to_dict(),
            ]
            report["partial"] = True
            report["outcome_unknown"] = True
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(
                    f"[log operation] code=plugin_logs_clear_failed exception={type(exc).__name__} "
                    f"trace={report.get('trace_id', '')}"
                )
            raise HTTPException(status_code=500, detail=report) from exc

        audit_ok = True
        try:
            webui_audit_log.record(
                action="plugin_logs_clear",
                qq=admin.qq,
                device_id=admin.device_id,
                target="plugin_runtime_logs",
                ip_hash=get_client_ip(request),
                detail={"deleted": deleted},
                outcome="ok",
            )
        except Exception as exc:
            audit_ok = False
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(f"[log operation] audit_failed exception={type(exc).__name__}")
        report = diagnostic(
            ok=True,
            code="plugin_logs_cleared",
            phase="operation_complete",
            title="插件日志已清空",
            message="持久日志清理已完成。",
            details=(detail("删除条数", deleted, "ok"),),
            steps=(
                step("persist", "清空持久日志", "ok", "日志删除事务已提交。"),
                step("audit", "记录管理员操作", "ok" if audit_ok else "warn", "审计记录已保存。" if audit_ok else "日志已清空，但审计记录写入失败。"),
            ),
            warnings=() if audit_ok else ("日志已清空，但本次管理员审计记录未能写入。",),
            retryable=False,
            partial=not audit_ok,
            outcome_unknown=False,
        )
        return {"deleted": deleted, **report}

    return router


__all__ = ["build_log_router"]
