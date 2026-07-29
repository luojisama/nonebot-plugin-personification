"""WebUI QQ 空间路由：展示发空间月度额度/状态，并支持管理员手动发一条。

让 agent 自己按额度把控发不发，这里把额度状态可视化给管理员，并提供手动控制。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from ...core import webui_audit_log
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, normalize_diagnostic, step
from ...core.runtime_identity import get_runtime_identity
from ...core.sensitive_data import sanitize_object, sanitize_text
from ..deps import AdminIdentity, get_client_ip, require_admin


_DIAGNOSTIC_FIELDS = (
    "ok",
    "code",
    "phase",
    "title",
    "message",
    "details",
    "steps",
    "warnings",
    "suggestion",
    "retryable",
    "partial",
    "outcome_unknown",
    "operation_id",
    "trace_id",
)
_HIDDEN_LAST_ERROR = "最近操作失败，详细原因仅保留在服务端脱敏日志中。"
_LOGIN_STATUS_MESSAGES = {
    "preparing": "正在生成二维码",
    "waiting_scan": "请使用手机 QQ 扫描二维码",
    "waiting_confirm": "已扫码，请在手机 QQ 中确认登录",
    "verifying": "登录已确认，正在验证 QZone 凭证",
    "success": "QZone 登录已恢复",
    "expired": "二维码已过期，请重新生成",
    "cancelled": "登录已取消",
    "risk_controlled": "腾讯拒绝了本次登录，请稍后重试或使用人工兜底",
    "account_mismatch": "扫码 QQ 与当前 Bot QQ 不一致",
    "failed": "登录会话未完成，请稍后重试",
}


def _attach_diagnostic(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["diagnostic"] = report
    for field in _DIAGNOSTIC_FIELDS:
        result.setdefault(field, report[field])
    return result


def _safe_last_error(value: Any) -> str:
    return _HIDDEN_LAST_ERROR if str(value or "").strip() else ""


def _safe_scan_result(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = sanitize_object(value)
    if not isinstance(result, dict):
        return {}
    if result.get("last_error"):
        result["last_error"] = _HIDDEN_LAST_ERROR
    return result


def _safe_login_result(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = sanitize_object(source)
    if not isinstance(result, dict):
        result = {}
    # session_id is intentionally returned because it is required by the bound QR workflow.
    result["session_id"] = sanitize_text(source.get("session_id") or "", limit=160)
    result["bot_id"] = sanitize_text(source.get("bot_id") or "", limit=32)
    status = sanitize_text(source.get("status") or "unknown", limit=48)
    result["status"] = status
    result["message"] = _LOGIN_STATUS_MESSAGES.get(status, "登录会话状态已更新")
    return result


def _exception_report(
    runtime: Any,
    exc: BaseException,
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    steps: tuple = (),
    operation_id: str = "",
    retryable: bool = True,
) -> dict[str, Any]:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion=suggestion,
        operation_id=operation_id,
        retryable=retryable,
    )
    report["code"] = code
    report["steps"] = [item.to_dict() for item in steps]
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        try:
            logger.warning(
                f"[webui.qzone] code={code} phase={phase} "
                f"exception={type(exc).__name__} trace={report.get('trace_id', '')}"
            )
        except Exception:
            pass
    return report


def _http_diagnostic(status_code: int, report: dict[str, Any]) -> HTTPException:
    return HTTPException(status_code=status_code, detail=report)


def _get_bot(runtime, bot_id: str = "") -> Any | None:
    try:
        bots = runtime.get_bots() if callable(getattr(runtime, "get_bots", None)) else {}
    except Exception:
        return None
    expected = str(bot_id or "").strip()
    if expected:
        for key, bot in (bots or {}).items():
            if str(getattr(bot, "self_id", key) or "") == expected:
                return bot
        return None
    return next(iter((bots or {}).values()), None)


def _auth_owner(admin: AdminIdentity) -> str:
    return f"{admin.qq}:{admin.device_id}"


def _bundle_attr(runtime, name: str) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    return getattr(bundle, name, None) if bundle is not None else None


def _safe_publish_operation(operation: Any) -> dict[str, Any]:
    source = operation if isinstance(operation, dict) else {}
    payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
    status = sanitize_text(source.get("status") or "unknown", limit=32)
    return {
        "operation_id": sanitize_text(source.get("operation_id") or "", limit=96),
        "bot_id": sanitize_text(source.get("bot_id") or "", limit=32),
        "kind": sanitize_text(source.get("kind") or "post", limit=32),
        "status": status,
        "created_at": float(source.get("created_at") or 0),
        "updated_at": float(source.get("updated_at") or 0),
        "dispatch_started_at": float(source.get("dispatch_started_at") or 0),
        "completed_at": float(source.get("completed_at") or 0),
        "result_code": sanitize_text(source.get("result_code") or "", limit=64),
        "remote_id": sanitize_text(source.get("remote_id") or "", limit=160),
        "content": sanitize_text(payload.get("content") or "", limit=200),
        "action_required": "verify_remote" if status == "unknown" else "wait",
    }


def _build_status(runtime) -> dict[str, Any]:
    from ...core.data_store import get_data_store
    from ...core.qzone_service import get_qzone_auth_status, get_qzone_capability_status
    from ...core.qzone_publish import list_qzone_publish_operations
    from ...core.time_ctx import get_configured_now
    from ...flows.qzone_social_flow import get_qzone_scan_status
    from ...jobs.periodic_jobs import build_qzone_quota

    cfg = getattr(runtime, "plugin_config", None)
    enabled = bool(getattr(cfg, "personification_qzone_enabled", False))
    proactive_enabled = bool(getattr(cfg, "personification_qzone_proactive_enabled", False))
    social_enabled = bool(getattr(cfg, "personification_qzone_social_enabled", False))
    inbound_enabled = bool(getattr(cfg, "personification_qzone_inbound_enabled", False))
    monthly_limit = int(getattr(cfg, "personification_qzone_monthly_limit", 30))
    min_interval_hours = float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0)
    check_interval = int(getattr(cfg, "personification_qzone_check_interval", 60) or 60)
    quiet_start = int(getattr(cfg, "personification_qzone_quiet_hour_start", 0) or 0)
    quiet_end = int(getattr(cfg, "personification_qzone_quiet_hour_end", 7) or 7)

    state = get_data_store().load_sync("qzone_post_state")
    if not isinstance(state, dict):
        state = {}
    now = get_configured_now()
    quota = build_qzone_quota(
        state=state,
        now=now,
        monthly_limit=monthly_limit,
        min_interval_hours=min_interval_hours,
    )
    last_post_at = float(state.get("last_post_at", 0) or 0)
    next_eligible_at = float(quota.get("next_eligible_at", 0) or 0)
    now_ts = time.time()
    social_state = get_data_store().load_sync("qzone_social_state")
    if not isinstance(social_state, dict):
        social_state = {}
    scheduler = _bundle_attr(runtime, "scheduler")
    try:
        bot_items = list((runtime.get_bots() or {}).items()) if callable(getattr(runtime, "get_bots", None)) else []
    except Exception:
        bot_items = []

    def _job_status(job_id: str) -> dict[str, Any]:
        try:
            job = scheduler.get_job(job_id) if scheduler is not None and hasattr(scheduler, "get_job") else None
        except Exception:
            job = None
        return {
            "registered": job is not None,
            "next_run_at": job.next_run_time.timestamp() if job is not None and getattr(job, "next_run_time", None) else 0,
        }

    def _safe_auth(bot_id: str = "") -> dict[str, Any]:
        value = sanitize_object(get_qzone_auth_status(bot_id))
        auth_value = value if isinstance(value, dict) else {}
        if auth_value.get("last_error"):
            auth_value["last_error"] = _HIDDEN_LAST_ERROR
        return auth_value

    auth_by_bot = {
        str(getattr(bot, "self_id", key) or key): _safe_auth(str(getattr(bot, "self_id", key) or key))
        for key, bot in bot_items
    }
    auth = next(iter(auth_by_bot.values()), _safe_auth())
    capabilities_by_bot = {
        str(getattr(bot, "self_id", key) or key): get_qzone_capability_status(
            str(getattr(bot, "self_id", key) or key),
            enabled=enabled,
        )
        for key, bot in bot_items
    }
    capabilities = next(
        iter(capabilities_by_bot.values()),
        get_qzone_capability_status("", enabled=enabled),
    )
    payload = {
        "enabled": enabled,
        "proactive_enabled": proactive_enabled,
        "social_enabled": social_enabled,
        "inbound_enabled": inbound_enabled,
        "publish_available": bool(capabilities.get("write_available")),
        "read_only": bool(capabilities.get("read_only")),
        "capabilities": capabilities,
        "capabilities_by_bot": capabilities_by_bot,
        "bots": [{"bot_id": str(getattr(bot, "self_id", key) or key)} for key, bot in bot_items],
        "cookie_configured": bool(str(getattr(cfg, "personification_qzone_cookie", "") or "").strip()),
        "auth": auth,
        "auth_by_bot": auth_by_bot,
        "scan": get_qzone_scan_status(),
        "social": {
            "last_scan_at": float(social_state.get("last_scan_at", 0) or 0),
            "last_result": _safe_scan_result(social_state.get("last_result")),
            "last_error": _safe_last_error(social_state.get("last_error")),
            "job": _job_status("personification_qzone_social_scan"),
        },
        "inbound": {
            "last_scan_at": float(social_state.get("last_inbound_scan_at", 0) or 0),
            "last_result": _safe_scan_result(social_state.get("last_inbound_result")),
            "last_error": _safe_last_error(social_state.get("last_inbound_error")),
            "job": _job_status("personification_qzone_inbound_poll"),
        },
        "quota": quota,
        "check_interval_minutes": check_interval,
        "quiet_hour_start": quiet_start,
        "quiet_hour_end": quiet_end,
        "last_post_at": last_post_at,
        "last_content": str(state.get("last_content", "") or ""),
        "next_eligible_at": next_eligible_at,
        "next_eligible_in_seconds": max(0, int(next_eligible_at - now_ts)) if next_eligible_at else 0,
        "recent_contents": [str(x) for x in list(state.get("recent_contents", []))][-12:],
        "server_now": int(now_ts),
        "runtime": get_runtime_identity(),
    }
    unresolved = [
        _safe_publish_operation(item)
        for item in list_qzone_publish_operations(limit=50)
        if str(item.get("status") or "") in {"reserved", "dispatching", "unknown"}
    ]
    payload["reconciliation"] = {
        "state": "unknown" if any(item["status"] == "unknown" for item in unresolved) else "in_progress" if unresolved else "clear",
        "blocking": bool(unresolved),
        "operations": unresolved[:20],
    }
    report = diagnostic(
        ok=True,
        code="qzone_status_loaded",
        phase="status_snapshot",
        title="QZone 运行状态已加载",
        message="额度、认证、扫描和调度状态已生成安全快照。",
        details=(
            detail("已连接 Bot", len(bot_items), "ok" if bot_items else "warn"),
            detail("qzone.web_write", capabilities["qzone.web_write"]["state"], "info"),
            detail("只读模式", payload["read_only"], "warn" if payload["read_only"] else "info"),
            detail("Build", payload["runtime"]["build_id"], "info"),
            detail("Worker", payload["runtime"]["worker_id"], "info"),
            detail("Process started", payload["runtime"]["process_started_at"], "info"),
        ),
        steps=(step("status_snapshot", "生成状态快照", "ok", "持久状态和运行时状态已完成脱敏。"),),
        retryable=False,
    )
    return _attach_diagnostic(payload, report)


def build_qzone_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/qzone", tags=["qzone"])

    @router.get("/status")
    async def status(response: Response, _: AdminIdentity = Depends(require_admin)) -> dict:
        response.headers["Cache-Control"] = "no-store, private"
        try:
            return _build_status(runtime)
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_status_exception",
                phase="status_snapshot",
                title="QZone 运行状态加载失败",
                message="服务器未能生成 QZone 状态快照。",
                suggestion="请根据 Trace ID 查看脱敏日志，修复状态存储或运行时依赖后重试。",
                steps=(step("status_snapshot", "生成状态快照", "error", "状态读取异常中断。"),),
            )
            raise _http_diagnostic(500, report) from exc

    @router.get("/operations/{operation_id}")
    async def get_operation(
        operation_id: str,
        response: Response,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_publish import get_qzone_publish_operation

        response.headers["Cache-Control"] = "no-store, private"
        operation = get_qzone_publish_operation(operation_id)
        if operation is None:
            report = diagnostic(
                ok=False,
                code="qzone_operation_not_found",
                phase="operation_lookup",
                title="没有找到空间发布操作",
                message="该 Operation ID 不存在或已不在当前数据存储中。",
                retryable=False,
                operation_id=str(operation_id or "")[:96],
            )
            raise _http_diagnostic(404, report)
        return {"ok": True, "operation": _safe_publish_operation(operation)}

    @router.post("/operations/{operation_id}/reconcile")
    async def reconcile_operation(
        operation_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_publish import reconcile_qzone_publish_from_self_feed

        bot_id = str(body.get("bot_id") or "").strip()
        bot = _get_bot(runtime, bot_id)
        service = _bundle_attr(runtime, "qzone_social_service")
        if bot is None or service is None:
            report = diagnostic(
                ok=False,
                code="qzone_reconcile_unavailable",
                phase="reconcile_preflight",
                title="空间对账能力不可用",
                message="目标 Bot 未连接或本人动态读取服务尚未初始化。",
                retryable=True,
                operation_id=str(operation_id or "")[:96],
            )
            raise _http_diagnostic(503, report)
        result = await reconcile_qzone_publish_from_self_feed(
            operation_id=operation_id,
            bot_id=str(getattr(bot, "self_id", bot_id) or bot_id),
            qzone_social_service=service,
        )
        changed = bool(result.get("changed"))
        ok = str(result.get("status") or "") == "succeeded"
        report = diagnostic(
            ok=ok,
            code="qzone_reconcile_succeeded" if changed else "qzone_reconcile_pending",
            phase="remote_reconciliation",
            title="空间发布已对账" if changed else "暂未确认空间发布结果",
            message="已在本人动态中找到唯一精确匹配并补齐本地记录。" if changed else "本人动态中暂未出现唯一精确匹配，operation 继续保持不可重发。",
            details=(detail("匹配数量", int(result.get("match_count", 1 if changed else 0)), "ok" if changed else "warn"),),
            suggestion="无需再次发布。" if changed else "稍后再次对账，或人工确认空间中确实不存在后再解除占用。",
            retryable=not changed,
            outcome_unknown=not changed,
            operation_id=str(operation_id or "")[:96],
        )
        webui_audit_log.record(
            action="qzone_publish_reconcile",
            qq=admin.qq,
            device_id=admin.device_id,
            target=bot_id,
            ip_hash=get_client_ip(request),
            detail={"operation_id": str(operation_id or "")[:96], "changed": changed},
            outcome="ok" if changed else "unknown",
        )
        return _attach_diagnostic({**result, "ok": ok}, report)

    @router.post("/operations/{operation_id}/resolve-absent")
    async def resolve_operation_absent(
        operation_id: str,
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_publish import resolve_qzone_publish_absent
        from ...core.time_ctx import get_configured_now

        bot_id = str(body.get("bot_id") or "").strip()
        result = resolve_qzone_publish_absent(
            operation_id=operation_id,
            bot_id=bot_id,
            now=get_configured_now(),
        )
        if str(result.get("status") or "") in {"not_found", "bot_conflict"}:
            report = diagnostic(
                ok=False,
                code="qzone_operation_resolution_conflict",
                phase="operation_resolution",
                title="无法解除空间发布占用",
                message="Operation ID 不存在或不属于所选 Bot。",
                retryable=False,
                operation_id=str(operation_id or "")[:96],
            )
            raise _http_diagnostic(409, report)
        changed = bool(result.get("changed"))
        report = diagnostic(
            ok=True,
            code="qzone_operation_confirmed_absent",
            phase="operation_resolution",
            title="已确认远端没有该条动态",
            message="operation 已结束为明确失败；原 Operation ID 不会被再次外发。",
            retryable=False,
            operation_id=str(operation_id or "")[:96],
        )
        webui_audit_log.record(
            action="qzone_publish_resolve_absent",
            qq=admin.qq,
            device_id=admin.device_id,
            target=bot_id,
            ip_hash=get_client_ip(request),
            detail={"operation_id": str(operation_id or "")[:96], "changed": changed},
            outcome="ok",
        )
        return _attach_diagnostic({**result, "ok": True}, report)

    @router.get("/reconcile-candidates")
    async def reconciliation_candidates(
        bot_id: str,
        response: Response,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store, private"
        bot = _get_bot(runtime, bot_id)
        service = _bundle_attr(runtime, "qzone_social_service")
        if bot is None or service is None:
            raise _http_diagnostic(503, diagnostic(
                ok=False,
                code="qzone_history_reconcile_unavailable",
                phase="history_reconcile_preflight",
                title="无法读取本人空间动态",
                message="目标 Bot 未连接或本人动态读取服务不可用。",
                retryable=True,
            ))
        ok, _message, feeds = await service.fetch_user_feeds(
            target_uin=str(getattr(bot, "self_id", bot_id) or bot_id),
            bot_id=str(getattr(bot, "self_id", bot_id) or bot_id),
            count=20,
            include_comments=False,
        )
        if not ok:
            raise _http_diagnostic(502, diagnostic(
                ok=False,
                code="qzone_history_reconcile_fetch_failed",
                phase="history_reconcile_fetch",
                title="本人空间动态读取失败",
                message="当前无法生成可供确认的漏记候选。",
                retryable=True,
            ))
        candidates = [
            {
                "feed_id": sanitize_text(item.get("feed_id") or "", limit=160),
                "content": sanitize_text(item.get("content") or "", limit=200),
                "created_at": float(item.get("created_at") or 0),
            }
            for item in feeds
            if isinstance(item, dict) and str(item.get("feed_id") or "") and str(item.get("content") or "").strip()
        ]
        return {"ok": True, "bot_id": bot_id, "candidates": candidates}

    @router.post("/reconcile-history")
    async def reconcile_historical_feed(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_publish import record_historical_qzone_feed
        from ...core.time_ctx import get_configured_now

        bot_id = str(body.get("bot_id") or "").strip()
        feed_id = str(body.get("feed_id") or "").strip()
        bot = _get_bot(runtime, bot_id)
        service = _bundle_attr(runtime, "qzone_social_service")
        if bot is None or service is None or not feed_id:
            raise _http_diagnostic(400, diagnostic(
                ok=False,
                code="qzone_history_reconcile_input_invalid",
                phase="history_reconcile_preflight",
                title="漏记补记参数无效",
                message="必须选择已连接 Bot 和一条远端动态。",
                retryable=False,
            ))
        ok, _message, feeds = await service.fetch_user_feeds(
            target_uin=bot_id,
            bot_id=bot_id,
            count=40,
            include_comments=False,
        )
        match = next((item for item in feeds if isinstance(item, dict) and str(item.get("feed_id") or "") == feed_id), None) if ok else None
        if match is None:
            raise _http_diagnostic(404, diagnostic(
                ok=False,
                code="qzone_history_feed_not_found",
                phase="history_reconcile_verify",
                title="远端动态已不存在",
                message="再次读取本人空间后没有找到所选动态，本次未修改本地账本。",
                retryable=True,
            ))
        now = get_configured_now()
        occurred_at = datetime.fromtimestamp(float(match.get("created_at") or now.timestamp()), tz=now.tzinfo)
        result = record_historical_qzone_feed(
            bot_id=bot_id,
            feed=match,
            occurred_at=occurred_at,
            resolved_at=now,
        )
        if not result.get("ok"):
            raise _http_diagnostic(409, diagnostic(
                ok=False,
                code="qzone_history_reconcile_conflict",
                phase="history_reconcile_commit",
                title="漏记动态无法补记",
                message="远端动态身份与目标 Bot 不一致，或动态缺少必要字段。",
                retryable=False,
            ))
        report = diagnostic(
            ok=True,
            code="qzone_history_reconciled",
            phase="history_reconcile_complete",
            title="漏记动态已补入本地账本",
            message="本月额度、上次发布时间和最近说说已按远端动态更新。",
            details=(detail("远端时间", int(result.get("remote_time") or 0), "ok"),),
            retryable=False,
            operation_id=str(result.get("operation_id") or ""),
        )
        webui_audit_log.record(
            action="qzone_history_reconcile",
            qq=admin.qq,
            device_id=admin.device_id,
            target=bot_id,
            ip_hash=get_client_ip(request),
            detail={"operation_id": result.get("operation_id"), "remote_id": feed_id[:160]},
            outcome="ok",
        )
        return _attach_diagnostic({**result, "ok": True}, report)

    @router.post("/post-now")
    async def post_now(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        """管理员手动强制发一条说说（绕过额度/间隔/agent 决策，但仍计入月度额度）。"""
        from ...core.time_ctx import get_configured_now
        from ...core.qzone_publish import qzone_content_preview
        from ...core.qzone_service import get_qzone_auth_status, get_qzone_capability_status
        from ...jobs.periodic_jobs import build_qzone_quota, coordinated_qzone_publish

        logger = getattr(runtime, "logger", None)
        operation_id = str(body.get("operation_id") or "").strip()[:96]
        if not operation_id:
            report = diagnostic(
                ok=False,
                code="qzone_operation_id_missing",
                phase="input_validation",
                title="缺少 Operation ID",
                message="发布请求必须携带稳定的 Operation ID。",
                steps=(step("operation_id", "校验 Operation ID", "error", "请求未提供 Operation ID。"),),
                suggestion="由调用端生成一次 Operation ID，并在同一次操作的查询或重试中保持不变。",
                retryable=False,
            )
            raise _http_diagnostic(400, report)
        try:
            generate = _bundle_attr(runtime, "qzone_generate_post")
            publish = _bundle_attr(runtime, "publish_qzone_shuo")
            update_cookie = _bundle_attr(runtime, "update_qzone_cookie")
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_post_preflight_exception",
                phase="post_preflight",
                title="QZone 发布预检异常",
                message="服务器未能读取 QZone 发布能力。",
                suggestion="请根据 Trace ID 检查运行时装配状态，修复后使用同一 Operation ID 重试。",
                steps=(step("capabilities", "检查发布能力", "error", "运行时能力读取异常中断。"),),
                operation_id=operation_id,
            )
            raise _http_diagnostic(500, report) from exc
        if generate is None or publish is None:
            report = diagnostic(
                ok=False,
                code="qzone_post_capability_unavailable",
                phase="post_preflight",
                title="QZone 发布能力未就绪",
                message="说说生成或发布能力尚未完成运行时初始化。",
                steps=(step("capabilities", "检查发布能力", "error", "必要能力不可用，未生成草稿。"),),
                suggestion="确认 QZone 已启用并重载插件运行时后重试。",
                retryable=True,
                operation_id=operation_id,
            )
            raise _http_diagnostic(503, report)

        bot = _get_bot(runtime, str(body.get("bot_id") or ""))
        if bot is None:
            report = diagnostic(
                ok=False,
                code="qzone_post_bot_unavailable",
                phase="post_preflight",
                title="目标 Bot 未连接",
                message="没有找到可执行本次 QZone 发布的已连接 Bot。",
                steps=(
                    step("capabilities", "检查发布能力", "ok", "生成与发布能力可用。"),
                    step("bot", "选择目标 Bot", "error", "目标 Bot 当前不可用。"),
                ),
                suggestion="等待目标 Bot 连接后，使用同一 Operation ID 重试。",
                retryable=True,
                operation_id=operation_id,
            )
            raise _http_diagnostic(503, report)

        warnings: list[str] = []
        auth_steps = []
        refresh_ok: bool | None = None
        if callable(update_cookie):
            try:
                refresh_ok, _refresh_message = await update_cookie(bot, force=True)
                if not refresh_ok:
                    warnings.append("LLOneBot Cookie 强制刷新未成功。")
                auth_steps.append(step(
                    "cookie_refresh",
                    "强制刷新 QZone 登录凭证",
                    "ok" if refresh_ok else "warn",
                    "已从当前 Bot 获取并验证最新 Cookie。" if refresh_ok else "未取得可验证的新 Cookie。",
                ))
            except Exception as exc:
                if logger is not None:
                    logger.warning(f"[webui.qzone] post cookie refresh exception={type(exc).__name__}")
                warnings.append("LLOneBot Cookie 强制刷新异常。")
                auth_steps.append(step(
                    "cookie_refresh",
                    "强制刷新 QZone 登录凭证",
                    "warn",
                    "自动刷新异常中断，未暴露底层凭证或响应。",
                ))
        else:
            auth_steps.append(step(
                "cookie_refresh",
                "强制刷新 QZone 登录凭证",
                "skipped",
                "当前 runtime 未提供 Cookie 刷新能力。",
            ))

        auth_status = get_qzone_auth_status(str(getattr(bot, "self_id", "") or ""))
        auth_preflight_status = str(auth_status.get("status") or "")
        if refresh_ok is not True and auth_preflight_status in {"login_required", "risk_blocked"}:
            risk_blocked = auth_preflight_status == "risk_blocked"
            result = diagnostic(
                ok=False,
                code="qzone_risk_blocked" if risk_blocked else "qzone_login_required",
                phase="qzone_auth",
                title="QZone 写操作触发安全验证" if risk_blocked else "QZone 登录凭证需要人工恢复",
                message=(
                    "腾讯仍要求安全验证；尚未生成草稿或提交发布。"
                    if risk_blocked
                    else "系统已自动尝试从当前 Bot 强制刷新 Cookie，但腾讯仍要求重新登录；尚未生成草稿或提交发布。"
                ),
                steps=tuple(auth_steps),
                warnings=warnings,
                suggestion=(
                    "暂停自动尝试并稍后人工确认 QZone 状态；不要高频刷新或发布。"
                    if risk_blocked
                    else "在上方“QZone 认证恢复”扫码一次；成功后重新发起发布。"
                ),
                retryable=False,
                operation_id=operation_id,
            )
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={"operation_id": operation_id, "code": result["code"], "phase": result["phase"]},
                outcome="failed",
            )
            return result

        first_party_publish = bool(getattr(publish, "supports_unknown_write_probe", False))
        capability_status = get_qzone_capability_status(
            str(getattr(bot, "self_id", "") or ""),
            enabled=bool(
                getattr(getattr(runtime, "plugin_config", None), "personification_qzone_enabled", False)
                or _bundle_attr(runtime, "qzone_publish_available")
            ),
        )
        web_write_state = str(
            (capability_status.get("qzone.web_write") or {}).get("state", "unknown")
        )
        if first_party_publish and web_write_state in {"disabled", "degraded", "unavailable"}:
            result = diagnostic(
                ok=False,
                code="qzone_web_write_unavailable",
                phase="qzone_capability",
                title="QZone 当前为只读模式",
                message="Web read 与普通 QQ 能力可继续使用，但写能力当前不可用或结果不可信。",
                details=(detail("qzone.web_write", web_write_state, "warn"),),
                steps=tuple(auth_steps) + (
                    step("capability_snapshot", "检查 QZone 写能力", "error", "写能力未处于 available。"),
                ),
                suggestion="先恢复认证或人工核对未知写结果；不要通过重复 POST 探测写能力。",
                retryable=False,
                operation_id=operation_id,
            )
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={"operation_id": operation_id, "code": result["code"], "web_write": web_write_state},
                outcome="failed",
            )
            return result

        try:
            detailed_generate = getattr(generate, "detailed", None)
            if callable(detailed_generate):
                generation = await detailed_generate(bot)
                content = str(generation.get("content") or "") if isinstance(generation, dict) else ""
                generation_diag = normalize_diagnostic(
                    generation.get("diagnostic") if isinstance(generation, dict) else None,
                    ok=bool(content),
                )
            else:
                content = await generate(bot)
                generation_diag = diagnostic(
                    ok=bool(content),
                    code="qzone_draft_ready" if content else "legacy_generator_empty",
                    phase="draft_generation",
                    title="说说草稿已生成" if content else "旧版生成器没有返回草稿",
                    message="草稿已生成。" if content else "当前运行时未提供详细生成报告，且返回正文为空。",
                    suggestion="重启插件加载最新运行时后再试。" if not content else "",
                    retryable=not bool(content),
                )
        except Exception as exc:
            result = diagnostic(
                ok=False,
                code="qzone_generation_exception",
                phase="draft_generation",
                title="说说生成流程异常中断",
                message="生成函数抛出异常，尚未进入 QZone 发布阶段。",
                details=(detail("异常类型", type(exc).__name__, "error"),),
                steps=tuple(auth_steps) + (step("draft_generation", "生成说说草稿", "error", "生成函数异常中断。"),),
                warnings=warnings,
                suggestion="根据异常类型检查生成链路和 Provider 状态后重试。",
                retryable=True,
                operation_id=operation_id,
            )
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={"operation_id": operation_id, "code": result["code"], "phase": result["phase"]},
                outcome="failed",
            )
            return result
        if not content:
            generation_diag["operation_id"] = operation_id
            generation_diag["warnings"] = list(generation_diag.get("warnings") or []) + warnings
            generation_diag["steps"] = [item.to_dict() for item in auth_steps] + list(generation_diag.get("steps") or [])
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={"operation_id": operation_id, "code": generation_diag.get("code"), "phase": generation_diag.get("phase")},
                outcome="failed",
            )
            return generation_diag

        cfg = getattr(runtime, "plugin_config", None)
        try:
            published = await coordinated_qzone_publish(
                operation_id=operation_id,
                content=content,
                bot_id=str(getattr(bot, "self_id", "") or ""),
                now=get_configured_now(),
                monthly_limit=int(getattr(cfg, "personification_qzone_monthly_limit", 30)),
                min_interval_hours=float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0),
                kind="post",
                publish=lambda: (
                    publish(
                        content,
                        getattr(bot, "self_id", ""),
                        allow_unknown_write=True,
                    )
                    if bool(getattr(publish, "supports_unknown_write_probe", False))
                    else publish(content, getattr(bot, "self_id", ""))
                ),
                force=True,
            )
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_publish_orchestration_exception",
                phase="qzone_publish",
                title="QZone 发布协调流程异常",
                message="发布协调器未返回可确认的操作状态。",
                suggestion="先按 Operation ID 检查服务端操作记录和 QZone 实际状态，不要创建新的重复请求。",
                steps=tuple(auth_steps) + (
                    step("draft_generation", "生成说说草稿", "ok", "草稿已生成。"),
                    step("publish", "提交到 QZone", "unknown", "发布协调流程异常中断，远端结果未知。"),
                ),
                operation_id=operation_id,
                retryable=False,
            )
            report["partial"] = True
            report["outcome_unknown"] = True
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={"operation_id": operation_id, "code": report["code"], "phase": report["phase"]},
                outcome="failed",
            )
            raise _http_diagnostic(500, report) from exc
        publish_result_code = sanitize_text(published.get("result_code") or published.get("status") or "unknown", limit=64)
        content_preview = qzone_content_preview(content, limit=200)
        raw_publish_detail = sanitize_object(published.get("publish_detail") or {})
        publish_detail = raw_publish_detail if isinstance(raw_publish_detail, dict) else {}
        image_requested = bool(publish_detail.get("image_requested"))
        image_uploaded = bool(publish_detail.get("image_uploaded"))
        image_upload_failure = publish_result_code.startswith("image_upload_")
        if not published.get("success"):
            raw_publish_status = str(published.get("status") or "failed")
            publish_status = raw_publish_status if raw_publish_status in {
                "definite_failure", "reserved", "dispatching", "outcome_unknown", "unknown",
                "quota_blocked", "interval_blocked", "payload_conflict", "unresolved_payload"
            } else "failed"
            auth_status = get_qzone_auth_status(str(getattr(bot, "self_id", "") or ""))
            auth_was_login_required = auth_status.get("status") == "login_required"
            auth_was_risk_blocked = auth_status.get("status") == "risk_blocked"
            recovery_step = None
            recovery_ok: bool | None = None
            if auth_was_login_required and callable(update_cookie):
                try:
                    recovery_ok, _recovery_message = await update_cookie(bot, force=True)
                    recovery_step = step(
                        "auth_recovery",
                        "发布失败后恢复登录凭证",
                        "ok" if recovery_ok else "error",
                        "已自动刷新并验证 Cookie；没有自动重发本次说说。" if recovery_ok else "自动刷新仍未取得有效 Cookie。",
                    )
                except Exception as exc:
                    if logger is not None:
                        logger.warning(f"[webui.qzone] post-auth recovery exception={type(exc).__name__}")
                    recovery_step = step(
                        "auth_recovery",
                        "发布失败后恢复登录凭证",
                        "error",
                        "自动刷新异常中断；没有自动重发本次说说。",
                    )
                warnings.append(
                    "检测到 QZone 登录失效，已自动刷新凭证；本次发布不会自动重发。"
                    if recovery_ok
                    else "检测到 QZone 登录失效，自动刷新仍未恢复；需要扫码登录。"
                )
            elif auth_was_risk_blocked:
                warnings.append("腾讯返回安全验证页面；已暂停该 Bot 的空间请求，没有自动刷新或重发。")
            outcome_unknown = publish_status in {"outcome_unknown", "unknown"}
            failure_phase = "qzone_publish"
            if outcome_unknown:
                code = "qzone_publish_outcome_unknown"
                title = "QZone 发布结果未知"
                message = "发布请求可能已经到达腾讯，但本次没有得到明确成功或失败结果。"
                suggestion = "先打开 QQ 空间检查是否已经发布，禁止直接再次点击发布，以免产生重复说说。"
                retryable = False
            elif publish_status in {"reserved", "dispatching"}:
                code = "qzone_publish_in_progress"
                title = "相同发布请求仍在处理中"
                message = "该 Operation ID 已有一个未完成的发布请求，当前没有再次向 QZone 外发。"
                suggestion = "等待原请求完成并刷新状态，不要创建新的重复请求。"
                retryable = False
            elif auth_was_login_required:
                code = "qzone_login_required"
                failure_phase = "qzone_auth"
                title = "QZone 登录凭证已失效"
                message = (
                    "腾讯要求重新登录；系统已自动刷新凭证，本次草稿没有自动重发。"
                    if recovery_ok
                    else "腾讯要求重新登录，自动刷新未恢复，本次草稿没有发布。"
                )
                suggestion = (
                    "认证已恢复，可以重新发起一次新的发布操作。"
                    if recovery_ok
                    else "在上方“QZone 认证恢复”扫码登录，确认认证健康后再重新发布。"
                )
                retryable = bool(recovery_ok)
            elif auth_was_risk_blocked:
                code = "qzone_risk_blocked"
                failure_phase = "qzone_auth"
                title = "QZone 写操作触发安全验证"
                message = "腾讯要求安全验证，本次没有继续提交说说。"
                suggestion = "暂停自动尝试并稍后人工确认 QZone 状态；不要高频刷新或发布。"
                retryable = False
            elif image_upload_failure:
                code = "qzone_image_upload_failed"
                failure_phase = "qzone_image_upload"
                title = "QZone 配图上传失败"
                message = "配图上传未完成，本次尚未提交说说正文。"
                suggestion = "检查配图格式和 QZone 上传状态后，可以安全地重新发起新的发布操作。"
                retryable = True
            else:
                code = "qzone_publish_rejected"
                title = "QZone 明确拒绝了发布"
                message = "发布层明确返回失败状态，本次未确认发布成功。"
                suggestion = "根据发布层返回和认证状态修复问题；只有明确失败时才可以重新发布。"
                retryable = True
            steps = [item.to_dict() for item in auth_steps] + list(generation_diag.get("steps") or [])
            publish_step_details = [detail("结果代码", publish_result_code, "warn" if outcome_unknown else "error")]
            if image_requested:
                publish_step_details.append(detail("配图上传", "已完成" if image_uploaded else "未完成", "ok" if image_uploaded else "error"))
            if publish_detail.get("image_mime_type"):
                publish_step_details.append(detail("配图格式", publish_detail.get("image_mime_type"), "info"))
            if publish_detail.get("image_converted") is not None:
                publish_step_details.append(detail("格式转换", "是" if publish_detail.get("image_converted") else "否", "info"))
            steps.append(step(
                "image_upload" if image_upload_failure else "publish",
                "上传 QZone 配图" if image_upload_failure else "提交到 QZone",
                "unknown" if outcome_unknown else "error",
                message,
                details=tuple(publish_step_details),
            ).to_dict())
            if recovery_step is not None:
                steps.append(recovery_step.to_dict())
            result = diagnostic(
                ok=False,
                code=code,
                phase=failure_phase,
                title=title,
                message=message,
                details=(
                    detail("候选正文", content_preview, "ok"),
                    detail("协调状态", publish_status, "error" if not outcome_unknown else "warn"),
                    detail("发布结果代码", publish_result_code, "warn" if outcome_unknown else "error"),
                ),
                steps=tuple(
                    step(
                        str(item.get("key") or "step"),
                        str(item.get("label") or "步骤"),
                        str(item.get("status") or "unknown"),
                        str(item.get("message") or ""),
                        details=tuple(
                            detail(str(child.get("label") or "详情"), child.get("value"), str(child.get("status") or "info"))
                            for child in item.get("details") or []
                            if isinstance(child, dict)
                        ),
                    )
                    for item in steps
                    if isinstance(item, dict)
                ),
                warnings=list(generation_diag.get("warnings") or []) + warnings,
                suggestion=suggestion,
                retryable=retryable,
                outcome_unknown=outcome_unknown,
                operation_id=operation_id,
            )
            result["content"] = content_preview
            webui_audit_log.record(
                action="qzone_post_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=str(getattr(bot, "self_id", "") or ""),
                ip_hash=get_client_ip(request),
                detail={
                    "operation_id": operation_id,
                    "code": code,
                    "status": publish_status,
                    "result_code": publish_result_code,
                },
                outcome="unknown" if outcome_unknown else "failed",
            )
            return result

        state = published.get("state") or {}
        mark_published = getattr(generate, "mark_published", None)
        if published.get("newly_committed") and callable(mark_published):
            mark_published(content)
        quota = build_qzone_quota(
            state=state,
            now=get_configured_now(),
            monthly_limit=int(getattr(cfg, "personification_qzone_monthly_limit", 30)),
            min_interval_hours=float(getattr(cfg, "personification_qzone_min_interval_hours", 12.0) or 0),
        )
        if logger is not None:
            logger.info(f"[webui] 管理员 {admin.qq} 手动发布了一条空间说说。")
        webui_audit_log.record(
            action="qzone_post_now",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(getattr(bot, "self_id", "") or ""),
            ip_hash=get_client_ip(request),
            detail={"operation_id": operation_id, "duplicate": bool(published.get("duplicate"))},
            outcome="ok",
        )
        generation_steps = [item.to_dict() for item in auth_steps] + list(generation_diag.get("steps") or [])
        success_publish_details = [detail("结果代码", publish_result_code, "ok")]
        if image_requested:
            success_publish_details.append(detail("配图上传", "已完成" if image_uploaded else "未完成", "ok" if image_uploaded else "warn"))
        if publish_detail.get("image_mime_type"):
            success_publish_details.append(detail("配图格式", publish_detail.get("image_mime_type"), "info"))
        generation_steps.append(step(
            "publish",
            "提交到 QZone",
            "ok",
            "腾讯已明确返回发布成功。",
            details=tuple(success_publish_details),
        ).to_dict())
        result = diagnostic(
            ok=True,
            code="qzone_post_published",
            phase="publish_complete",
            title="说说已经发布",
            message="草稿通过全部检查，腾讯已明确确认发布成功。",
            details=(
                detail("正文", content_preview, "ok"),
                detail("发布结果代码", publish_result_code, "ok"),
                detail("本月已用额度", quota.get("used", 0), "info"),
            ),
            steps=tuple(
                step(
                    str(item.get("key") or "step"),
                    str(item.get("label") or "步骤"),
                    str(item.get("status") or "unknown"),
                    str(item.get("message") or ""),
                    details=tuple(
                        detail(str(child.get("label") or "详情"), child.get("value"), str(child.get("status") or "info"))
                        for child in item.get("details") or []
                        if isinstance(child, dict)
                    ),
                )
                for item in generation_steps
                if isinstance(item, dict)
            ),
            warnings=list(generation_diag.get("warnings") or []) + warnings,
            suggestion="无需再次点击发布。",
            operation_id=operation_id,
        )
        result.update({
            "content": content_preview,
            "quota": quota,
            "duplicate": bool(published.get("duplicate")),
        })
        return result

    @router.post("/refresh-cookie")
    async def refresh_cookie(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            update_cookie = _bundle_attr(runtime, "update_qzone_cookie")
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_cookie_refresh_preflight_exception",
                phase="cookie_refresh_preflight",
                title="Cookie 刷新预检异常",
                message="服务器未能读取 Cookie 刷新能力。",
                suggestion="请根据 Trace ID 检查运行时装配状态后重试。",
                steps=(step("capability", "检查刷新能力", "error", "运行时能力读取异常中断。"),),
            )
            raise _http_diagnostic(500, report) from exc
        bot = _get_bot(runtime, str(body.get("bot_id") or ""))
        if not callable(update_cookie):
            report = diagnostic(
                ok=False,
                code="qzone_cookie_refresh_unavailable",
                phase="cookie_refresh_preflight",
                title="Cookie 刷新能力未就绪",
                message="当前运行时没有可调用的 Cookie 刷新能力。",
                steps=(step("capability", "检查刷新能力", "error", "刷新能力不可用。"),),
                suggestion="确认 QZone 已启用并重载插件运行时后重试。",
                retryable=True,
            )
            raise _http_diagnostic(503, report)
        if bot is None:
            report = diagnostic(
                ok=False,
                code="qzone_cookie_refresh_bot_unavailable",
                phase="cookie_refresh_preflight",
                title="目标 Bot 未连接",
                message="没有找到可用于刷新 QZone Cookie 的已连接 Bot。",
                steps=(
                    step("capability", "检查刷新能力", "ok", "刷新能力可用。"),
                    step("bot", "选择目标 Bot", "error", "目标 Bot 当前不可用。"),
                ),
                suggestion="等待目标 Bot 连接后重试。",
                retryable=True,
            )
            raise _http_diagnostic(503, report)
        refresh_exc: BaseException | None = None
        try:
            ok, _service_message = await update_cookie(bot, force=True)
        except Exception as exc:
            ok = False
            refresh_exc = exc
        if refresh_exc is not None:
            report = _exception_report(
                runtime,
                refresh_exc,
                code="qzone_cookie_refresh_exception",
                phase="cookie_refresh",
                title="Cookie 刷新异常中断",
                message="Cookie 刷新调用发生内部异常，未返回可用凭证。",
                suggestion="请根据 Trace ID 检查 OneBot 连接与刷新能力后重试。",
                steps=(
                    step("capability", "检查刷新能力", "ok", "刷新能力可用。"),
                    step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("refresh_cookie", "刷新 Cookie", "error", "刷新调用异常中断。"),
                ),
            )
        elif ok:
            report = diagnostic(
                ok=True,
                code="qzone_cookie_refreshed",
                phase="cookie_refresh",
                title="QZone Cookie 已刷新",
                message="LLOneBot 返回的凭证已完成验证和安装。",
                steps=(
                    step("capability", "检查刷新能力", "ok", "刷新能力可用。"),
                    step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("refresh_cookie", "刷新 Cookie", "ok", "凭证已验证并安装。"),
                ),
                retryable=False,
            )
        else:
            report = diagnostic(
                ok=False,
                code="qzone_cookie_refresh_failed",
                phase="cookie_refresh",
                title="QZone Cookie 刷新失败",
                message="刷新能力明确返回失败，现有认证状态未被确认恢复。",
                steps=(
                    step("capability", "检查刷新能力", "ok", "刷新能力可用。"),
                    step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("refresh_cookie", "刷新 Cookie", "error", "刷新能力返回失败状态。"),
                ),
                suggestion="检查 OneBot 登录状态，必要时使用 QZone 扫码认证恢复。",
                retryable=True,
            )
        webui_audit_log.record(
            action="qzone_cookie_refresh",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(getattr(bot, "self_id", "") or ""),
            ip_hash=get_client_ip(request),
            detail={"ok": bool(ok), "status": "refreshed" if ok else "failed"},
            outcome="ok" if ok else "failed",
        )
        return _attach_diagnostic(
            {
                "ok": bool(ok),
                "status": "refreshed" if ok else "failed",
                "message": "ok" if ok else "Cookie 刷新失败",
            },
            report,
        )

    @router.post("/auth/login/start")
    async def start_login(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_auth import qzone_login_manager
        from ...core.qzone_service import install_qzone_cookie

        bot = _get_bot(runtime, str(body.get("bot_id") or ""))
        if bot is None:
            report = diagnostic(
                ok=False,
                code="qzone_login_bot_unavailable",
                phase="login_start",
                title="目标 Bot 未连接",
                message="没有找到可绑定本次 QZone 登录会话的已连接 Bot。",
                steps=(step("bot", "选择目标 Bot", "error", "目标 Bot 当前不可用。"),),
                suggestion="等待目标 Bot 连接后重试。",
                retryable=True,
            )
            raise _http_diagnostic(503, report)
        bot_id = str(getattr(bot, "self_id", "") or "")

        async def _install(cookie: str, expected_bot_id: str, source: str) -> tuple[bool, str]:
            return await install_qzone_cookie(
                cookie=cookie,
                expected_bot_id=expected_bot_id,
                plugin_config=getattr(runtime, "plugin_config", None),
                logger=getattr(runtime, "logger", None),
                source=source,
            )

        try:
            result = await qzone_login_manager.start(
                bot_id=bot_id,
                owner_key=_auth_owner(admin),
                install_cookie=_install,
            )
        except RuntimeError as exc:
            webui_audit_log.record(
                action="qzone_login_start",
                qq=admin.qq,
                device_id=admin.device_id,
                target=bot_id,
                ip_hash=get_client_ip(request),
                detail={"status": "rate_limited_or_busy"},
                outcome="failed",
            )
            report = _exception_report(
                runtime,
                exc,
                code="qzone_login_start_blocked",
                phase="login_start",
                title="暂时不能创建登录会话",
                message="登录恢复当前受限频或其它活动会话保护。",
                suggestion="等待当前会话结束或短暂冷却后重试，不要高频创建二维码。",
                steps=(
                    step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("create_session", "创建登录会话", "error", "会话保护拒绝了本次请求。"),
                ),
                retryable=True,
            )
            raise _http_diagnostic(429, report) from exc
        except Exception as exc:
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(f"[qzone-auth] 生成登录二维码失败：{type(exc).__name__}")
            webui_audit_log.record(
                action="qzone_login_start",
                qq=admin.qq,
                device_id=admin.device_id,
                target=bot_id,
                ip_hash=get_client_ip(request),
                detail={"status": "upstream_failed", "error_type": type(exc).__name__},
                outcome="failed",
            )
            report = _exception_report(
                runtime,
                exc,
                code="qzone_login_start_exception",
                phase="login_start",
                title="QZone 登录会话创建失败",
                message="服务器未能完成腾讯登录二维码初始化。",
                suggestion="请根据 Trace ID 检查网络和 Tencent 登录协议状态后重试。",
                steps=(
                    step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("create_session", "创建登录会话", "ok", "服务端会话已创建。"),
                    step("request_qrcode", "请求登录二维码", "error", "二维码初始化异常中断。"),
                ),
                retryable=True,
            )
            raise _http_diagnostic(502, report) from exc
        safe_result = _safe_login_result(result)
        webui_audit_log.record(
            action="qzone_login_start",
            qq=admin.qq,
            device_id=admin.device_id,
            target=bot_id,
            ip_hash=get_client_ip(request),
            detail={"status": safe_result.get("status")},
            outcome="ok",
        )
        report = diagnostic(
            ok=True,
            code="qzone_login_started",
            phase="login_start",
            title="QZone 登录二维码已生成",
            message="登录会话已绑定当前管理员设备与目标 Bot。",
            details=(detail("会话状态", safe_result.get("status") or "unknown", "info"),),
            steps=(
                step("bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                step("create_session", "创建登录会话", "ok", "服务端会话已创建并绑定。"),
                step("request_qrcode", "请求登录二维码", "ok", "二维码已保存在服务端内存。"),
            ),
            suggestion="使用手机 QQ 扫码，并在会话过期前完成确认。",
            retryable=False,
        )
        return _attach_diagnostic(safe_result, report)

    @router.get("/auth/login/{session_id}/status")
    async def login_status(
        session_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_auth import qzone_login_manager

        try:
            result = qzone_login_manager.status(session_id, owner_key=_auth_owner(admin))
        except LookupError as exc:
            report = diagnostic(
                ok=False,
                code="qzone_login_session_not_found",
                phase="login_status",
                title="登录会话不存在或已过期",
                message="当前管理员设备无法访问该登录会话。",
                steps=(step("load_session", "读取登录会话", "error", "没有找到匹配的有效会话。"),),
                suggestion="重新创建 QZone 登录二维码。",
                retryable=False,
            )
            raise _http_diagnostic(404, report) from exc
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_login_status_exception",
                phase="login_status",
                title="登录会话状态读取失败",
                message="服务器未能读取当前登录会话状态。",
                suggestion="请根据 Trace ID 检查登录会话管理器后重试。",
                steps=(step("load_session", "读取登录会话", "error", "会话状态读取异常中断。"),),
            )
            raise _http_diagnostic(500, report) from exc
        safe_result = _safe_login_result(result)
        terminal = bool(safe_result.get("terminal"))
        report = diagnostic(
            ok=True,
            code="qzone_login_status_loaded",
            phase="login_status",
            title="登录会话状态已更新",
            message="已读取当前设备绑定的 QZone 登录会话状态。",
            details=(detail("会话状态", safe_result.get("status") or "unknown", "info"),),
            steps=(step("load_session", "读取登录会话", "ok", "会话状态已安全返回。"),),
            suggestion="会话已结束。" if terminal else "继续按页面提示完成扫码确认。",
            retryable=False,
        )
        return _attach_diagnostic(safe_result, report)

    @router.get("/auth/login/{session_id}/qrcode")
    async def login_qrcode(
        session_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> Response:
        from ...core.qzone_auth import qzone_login_manager

        try:
            image = qzone_login_manager.qrcode(session_id, owner_key=_auth_owner(admin))
        except LookupError as exc:
            report = diagnostic(
                ok=False,
                code="qzone_login_qrcode_not_found",
                phase="login_qrcode",
                title="登录二维码不存在或已失效",
                message="当前管理员设备无法读取该二维码。",
                steps=(step("load_qrcode", "读取登录二维码", "error", "没有找到可用二维码。"),),
                suggestion="重新创建 QZone 登录二维码。",
                retryable=False,
            )
            raise _http_diagnostic(404, report) from exc
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_login_qrcode_exception",
                phase="login_qrcode",
                title="登录二维码读取失败",
                message="服务器未能读取当前登录二维码。",
                suggestion="请根据 Trace ID 检查登录会话管理器后重试。",
                steps=(step("load_qrcode", "读取登录二维码", "error", "二维码读取异常中断。"),),
            )
            raise _http_diagnostic(500, report) from exc
        return Response(
            content=image,
            media_type="image/png",
            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache"},
        )

    @router.post("/auth/login/{session_id}/cancel")
    async def cancel_login(
        session_id: str,
        request: Request,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_auth import qzone_login_manager

        try:
            result = await qzone_login_manager.cancel(session_id, owner_key=_auth_owner(admin))
        except LookupError as exc:
            report = diagnostic(
                ok=False,
                code="qzone_login_cancel_not_found",
                phase="login_cancel",
                title="登录会话不存在或已过期",
                message="当前管理员设备无法取消该登录会话。",
                steps=(
                    step("load_session", "读取登录会话", "error", "没有找到匹配的有效会话。"),
                    step("cancel_session", "取消登录会话", "skipped", "未执行取消。"),
                ),
                suggestion="刷新页面确认状态，必要时重新创建登录会话。",
                retryable=False,
            )
            raise _http_diagnostic(404, report) from exc
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_login_cancel_exception",
                phase="login_cancel",
                title="登录会话取消失败",
                message="服务器未能完成登录会话清理。",
                suggestion="请根据 Trace ID 检查会话状态后重试取消。",
                steps=(
                    step("load_session", "读取登录会话", "ok", "会话已定位。"),
                    step("cancel_session", "取消登录会话", "error", "会话清理异常中断。"),
                ),
            )
            raise _http_diagnostic(500, report) from exc
        safe_result = _safe_login_result(result)
        webui_audit_log.record(
            action="qzone_login_cancel",
            qq=admin.qq,
            device_id=admin.device_id,
            target=str(safe_result.get("bot_id") or ""),
            ip_hash=get_client_ip(request),
            detail={"status": "cancelled"},
            outcome="ok",
        )
        report = diagnostic(
            ok=True,
            code="qzone_login_cancelled",
            phase="login_cancel",
            title="QZone 登录会话已取消",
            message="二维码、临时 token 和登录连接已按会话状态清理。",
            details=(detail("会话状态", safe_result.get("status") or "cancelled", "ok"),),
            steps=(
                step("load_session", "读取登录会话", "ok", "会话已定位。"),
                step("cancel_session", "取消登录会话", "ok", "会话资源已清理。"),
            ),
            retryable=False,
        )
        return _attach_diagnostic(safe_result, report)

    @router.post("/auth/cookie")
    async def import_cookie(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        from ...core.qzone_service import install_qzone_cookie

        cookie = str(body.get("cookie") or "").strip()
        if not cookie or len(cookie) > 16_384:
            report = diagnostic(
                ok=False,
                code="qzone_cookie_input_invalid",
                phase="cookie_validation",
                title="Cookie 输入无效",
                message="Cookie 为空或超过允许的 16 KiB。",
                steps=(step("validate_input", "校验 Cookie 输入", "error", "输入长度不符合要求。"),),
                suggestion="粘贴目标 Bot 的完整 QZone Cookie 后重试。",
                retryable=False,
            )
            raise _http_diagnostic(400, report)
        bot = _get_bot(runtime, str(body.get("bot_id") or ""))
        if bot is None:
            report = diagnostic(
                ok=False,
                code="qzone_cookie_import_bot_unavailable",
                phase="cookie_validation",
                title="目标 Bot 未连接",
                message="没有找到可用于校验 Cookie 身份的已连接 Bot。",
                steps=(
                    step("validate_input", "校验 Cookie 输入", "ok", "输入长度符合要求。"),
                    step("select_bot", "选择目标 Bot", "error", "目标 Bot 当前不可用。"),
                ),
                suggestion="等待目标 Bot 连接后重新粘贴 Cookie。",
                retryable=True,
            )
            raise _http_diagnostic(503, report)
        bot_id = str(getattr(bot, "self_id", "") or "")
        try:
            ok, reason = await install_qzone_cookie(
                cookie=cookie,
                expected_bot_id=bot_id,
                plugin_config=getattr(runtime, "plugin_config", None),
                logger=getattr(runtime, "logger", None),
                source="manual",
            )
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_cookie_import_exception",
                phase="cookie_install",
                title="Cookie 验证安装异常",
                message="服务器未能完成 Cookie 身份验证和安装。",
                suggestion="请根据 Trace ID 检查认证探测和配置持久化状态后重试。",
                steps=(
                    step("validate_input", "校验 Cookie 输入", "ok", "输入长度符合要求。"),
                    step("select_bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("verify_cookie", "验证 Cookie", "error", "认证验证异常中断。"),
                    step("install_cookie", "安装 Cookie", "skipped", "未确认安装。"),
                ),
            )
            webui_audit_log.record(
                action="qzone_cookie_import",
                qq=admin.qq,
                device_id=admin.device_id,
                target=bot_id,
                ip_hash=get_client_ip(request),
                detail={"source": "manual", "status": "exception", "code": report["code"]},
                outcome="failed",
            )
            raise _http_diagnostic(500, report) from exc
        reason_code = str(reason or "")
        messages = {
            "missing_p_skey": "Cookie 缺少 p_skey",
            "missing_uin": "Cookie 缺少有效 uin",
            "account_mismatch": "Cookie QQ 与当前 Bot QQ 不一致",
            "auth_blocked": "Cookie 已失效或仍被腾讯认证拦截",
            "probe_failed": "暂时无法验证 Cookie，请稍后重试",
        }
        message = "QZone Cookie 已验证并安装" if ok else messages.get(reason_code, "Cookie 验证失败")
        failure_codes = {
            "missing_p_skey": "qzone_cookie_missing_p_skey",
            "missing_uin": "qzone_cookie_missing_uin",
            "account_mismatch": "qzone_cookie_account_mismatch",
            "auth_blocked": "qzone_cookie_auth_blocked",
            "probe_failed": "qzone_cookie_probe_failed",
        }
        safe_reason = reason_code if reason_code in failure_codes else "validation_failed"
        webui_audit_log.record(
            action="qzone_cookie_import",
            qq=admin.qq,
            device_id=admin.device_id,
            target=bot_id,
            ip_hash=get_client_ip(request),
            detail={"source": "manual", "status": "installed" if ok else safe_reason},
            outcome="ok" if ok else "failed",
        )
        if ok:
            report = diagnostic(
                ok=True,
                code="qzone_cookie_installed",
                phase="cookie_install",
                title="QZone Cookie 已安装",
                message="Cookie 身份、目标 Bot 和只读 QZone 探测均已验证。",
                steps=(
                    step("validate_input", "校验 Cookie 输入", "ok", "输入长度符合要求。"),
                    step("select_bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("verify_cookie", "验证 Cookie", "ok", "账号身份和 QZone 认证探测已通过。"),
                    step("install_cookie", "安装 Cookie", "ok", "运行时配置已更新。"),
                ),
                retryable=False,
            )
        else:
            report = diagnostic(
                ok=False,
                code=failure_codes.get(reason_code, "qzone_cookie_validation_failed"),
                phase="cookie_validation",
                title="QZone Cookie 验证失败",
                message=message,
                details=(detail("安全原因码", safe_reason, "error"),),
                steps=(
                    step("validate_input", "校验 Cookie 输入", "ok", "输入长度符合要求。"),
                    step("select_bot", "选择目标 Bot", "ok", "目标 Bot 已连接。"),
                    step("verify_cookie", "验证 Cookie", "error", "Cookie 未通过账号或只读认证探测。"),
                    step("install_cookie", "安装 Cookie", "skipped", "无效凭证未写入配置。"),
                ),
                suggestion="按安全原因码修正凭证，或改用手机 QQ 扫码恢复登录。",
                retryable=reason_code == "probe_failed",
            )
        return _attach_diagnostic(
            {"ok": bool(ok), "status": "installed" if ok else "failed", "message": message},
            report,
        )

    @router.post("/scan-now")
    async def scan_now(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        kind = str(body.get("kind") or "inbound").strip().lower()
        if kind not in {"social", "inbound"}:
            report = diagnostic(
                ok=False,
                code="qzone_scan_kind_invalid",
                phase="scan_preflight",
                title="扫描类型无效",
                message="kind 只能是 social 或 inbound。",
                steps=(step("validate_kind", "校验扫描类型", "error", "扫描类型不受支持。"),),
                suggestion="选择好友动态扫描或留言轮询。",
                retryable=False,
            )
            raise _http_diagnostic(400, report)
        try:
            runner = _bundle_attr(runtime, "qzone_social_scan" if kind == "social" else "qzone_inbound_poll")
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code="qzone_scan_preflight_exception",
                phase="scan_preflight",
                title="QZone 扫描预检异常",
                message="服务器未能读取对应扫描任务能力。",
                suggestion="请根据 Trace ID 检查运行时装配状态后重试。",
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "error", "运行时能力读取异常中断。"),
                ),
            )
            raise _http_diagnostic(500, report) from exc
        if not callable(runner):
            report = diagnostic(
                ok=False,
                code="qzone_scan_unavailable",
                phase="scan_preflight",
                title="QZone 扫描任务未就绪",
                message="对应扫描任务尚未完成运行时初始化。",
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "error", "扫描能力不可用。"),
                ),
                suggestion="确认对应 QZone 功能已启用并重载插件运行时后重试。",
                retryable=True,
            )
            raise _http_diagnostic(503, report)
        try:
            raw_result = await runner(force=True)
        except Exception as exc:
            report = _exception_report(
                runtime,
                exc,
                code=f"qzone_{kind}_scan_exception",
                phase="scan_execute",
                title="QZone 扫描异常中断",
                message="扫描任务发生内部异常，未返回可用结果。",
                suggestion="请根据 Trace ID 检查扫描任务与认证状态后重试。",
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "ok", "扫描能力可用。"),
                    step("run_scan", "执行 QZone 扫描", "error", "扫描任务异常中断。"),
                ),
            )
            webui_audit_log.record(
                action="qzone_scan_now",
                qq=admin.qq,
                device_id=admin.device_id,
                target=kind,
                ip_hash=get_client_ip(request),
                detail={"status": "exception", "code": report["code"]},
                outcome="failed",
            )
            raise _http_diagnostic(500, report) from exc
        result = _safe_scan_result(raw_result)
        skipped = bool(result.get("skipped"))
        ok = bool(result.get("ok"))
        if skipped:
            report = diagnostic(
                ok=True,
                code="qzone_scan_skipped_busy",
                phase="scan_skipped",
                title="QZone 扫描已安全跳过",
                message="另一轮扫描正在持有共享租约，本次没有重复启动。",
                details=(detail("扫描类型", kind, "info"),),
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "ok", "扫描能力可用。"),
                    step("run_scan", "执行 QZone 扫描", "skipped", "共享扫描租约正忙。"),
                ),
                suggestion="等待当前扫描完成后刷新状态。",
                retryable=False,
            )
        elif ok:
            report = diagnostic(
                ok=True,
                code=f"qzone_{kind}_scan_completed",
                phase="scan_complete",
                title="QZone 扫描已完成",
                message="扫描任务已返回明确完成状态。",
                details=(
                    detail("扫描类型", kind, "info"),
                    detail("发现动态", int(result.get("feeds_seen", 0) or 0), "info"),
                    detail("失败项目", int(result.get("failed", 0) or 0), "warn" if result.get("failed") else "info"),
                ),
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "ok", "扫描能力可用。"),
                    step("run_scan", "执行 QZone 扫描", "ok", "扫描任务已完成。"),
                ),
                retryable=False,
            )
        else:
            report = diagnostic(
                ok=False,
                code=f"qzone_{kind}_scan_failed",
                phase="scan_execute",
                title="QZone 扫描未完成",
                message="扫描任务明确返回失败状态，底层 service message 未向客户端回显。",
                details=(detail("扫描类型", kind, "info"),),
                steps=(
                    step("validate_kind", "校验扫描类型", "ok", f"已选择 {kind}。"),
                    step("capability", "检查扫描能力", "ok", "扫描能力可用。"),
                    step("run_scan", "执行 QZone 扫描", "error", "扫描任务返回失败状态。"),
                ),
                suggestion="检查 QZone 认证与服务端脱敏日志后重试。",
                retryable=True,
            )
        webui_audit_log.record(
            action="qzone_scan_now",
            qq=admin.qq,
            device_id=admin.device_id,
            target=kind,
            ip_hash=get_client_ip(request),
            detail={"status": report["code"], "skipped": skipped},
            outcome="ok" if ok else "failed",
        )
        return _attach_diagnostic(result, report)

    return router
