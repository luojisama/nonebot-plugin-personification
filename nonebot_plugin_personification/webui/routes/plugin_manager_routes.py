from __future__ import annotations

import re
import uuid
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ...core import plugin_update_manager, webui_audit_log
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ...core.sensitive_data import sanitize_text
from ..deps import AdminIdentity, require_admin


_URL_PATTERN = re.compile(r"[a-z][a-z0-9+.-]*://[^\s<>\"']+", re.IGNORECASE)
_SCP_REMOTE_PATTERN = re.compile(r"\b[^\s/@:]+@([a-z0-9.-]+):[^\s]+", re.IGNORECASE)
_DIAGNOSTIC_FIELDS = (
    "ok",
    "code",
    "phase",
    "title",
    "message",
    "details",
    "steps",
    "retryable",
    "partial",
    "outcome_unknown",
)


def _safe_endpoint(value: Any) -> str:
    raw = sanitize_text(value, limit=500).strip()
    if not raw:
        return ""
    scp_match = re.match(r"^[^\s/@:]+@([a-z0-9.-]+):", raw, re.IGNORECASE)
    if scp_match:
        return f"ssh://{scp_match.group(1)}/..."
    try:
        parsed = urlsplit(raw)
        if parsed.scheme and parsed.hostname:
            try:
                port = f":{parsed.port}" if parsed.port else ""
            except ValueError:
                port = ""
            return f"{parsed.scheme.lower()}://{parsed.hostname}{port}/..."
    except ValueError:
        pass
    return "<configured endpoint>"


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = sanitize_text(value, limit=limit)
    text = _URL_PATTERN.sub(lambda match: _safe_endpoint(match.group(0)), text)
    return _SCP_REMOTE_PATTERN.sub(lambda match: f"ssh://{match.group(1)}/...", text)


def _sanitize_manager_value(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {str(item_key): _sanitize_manager_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_manager_value(item, key=key) for item in value]
    if isinstance(value, str):
        lowered = key.lower()
        if "url" in lowered or "mirror" in lowered:
            return _safe_endpoint(value)
        return _safe_text(value, limit=2000)
    return value


def _short_sha(status: dict[str, Any], side: str = "local") -> str:
    revision = status.get(side) if isinstance(status.get(side), dict) else {}
    value = str((revision or {}).get("short_hash") or (revision or {}).get("hash") or "")
    return value[:12]


def _repository_step(status: dict[str, Any], *, key: str, label: str):
    available = bool(status.get("available"))
    dirty = bool(status.get("dirty"))
    source = status.get("source") if isinstance(status.get("source"), dict) else {}
    if not available:
        step_status = "error"
        message = "未能识别可更新的 Git repository。"
    elif dirty:
        step_status = "warn"
        message = "Repository 可读取，但 worktree 存在未提交改动。"
    else:
        step_status = "ok"
        message = "Repository 与 worktree 状态已读取。"
    return step(
        key,
        label,
        step_status,
        message,
        details=(
            detail("安装源", status.get("source_type") or "unknown"),
            detail("分支", source.get("branch") or _short_sha(status) or "未识别"),
            detail("Upstream", source.get("upstream") or "未配置", "warn" if not source.get("upstream") else "info"),
            detail("Worktree", f"dirty ({int(status.get('dirty_count') or 0)})" if dirty else "clean", "warn" if dirty else "ok"),
        ),
    )


def _remote_steps(status: dict[str, Any]) -> tuple:
    fetch = status.get("fetch") if isinstance(status.get("fetch"), dict) else {}
    probes = [item for item in fetch.get("probes") or [] if isinstance(item, dict)]
    attempted = bool(fetch.get("attempted"))
    available_probes = sum(1 for item in probes if item.get("ok"))
    used_mirror = str(fetch.get("used_mirror") or "")
    if probes:
        probe_status = "ok" if used_mirror else ("warn" if fetch.get("ok") else "error")
        probe_message = (
            "已选择通过 probe 的 mirror。"
            if used_mirror
            else "Mirror 未被采用，已尝试默认 remote。"
        )
    else:
        probe_status = "skipped"
        probe_message = "未配置 mirror probe，使用默认 remote。" if attempted else "本次未执行 mirror probe。"
    probe_step = step(
        "mirror_probe",
        "Mirror / endpoint probe",
        probe_status,
        probe_message,
        details=(
            detail("Probe 数量", len(probes)),
            detail("可用数量", available_probes, "ok" if available_probes else "info"),
            detail("选用 endpoint", _safe_endpoint(used_mirror) or "default remote"),
        ),
    )
    if not attempted:
        fetch_status = "skipped"
        fetch_message = "本次未请求远端更新引用。"
    elif fetch.get("ok"):
        fetch_status = "ok"
        fetch_message = "Remote refs 已刷新。"
    else:
        fetch_status = "error"
        fetch_message = "Remote fetch 未完成。"
    fetch_details = []
    if fetch.get("error"):
        fetch_details.append(detail("失败摘要", _safe_text(fetch.get("error"), limit=300), "error"))
    return probe_step, step("remote_fetch", "Fetch remote refs", fetch_status, fetch_message, details=fetch_details)


def _compare_step(status: dict[str, Any]):
    remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
    remote_error = str((remote or {}).get("error") or "")
    return step(
        "revision_compare",
        "Compare revisions",
        "error" if remote_error else "ok",
        "已比较 local HEAD 与 upstream。" if not remote_error else "无法完整读取 upstream revision。",
        details=(
            detail("Local SHA", _short_sha(status) or "unknown"),
            detail("Remote SHA", _short_sha(status, "remote") or "unknown", "warn" if remote_error else "info"),
            detail("Ahead / behind", f"{int(status.get('ahead') or 0)} / {int(status.get('behind') or 0)}"),
        ),
    )


def _check_diagnostic(status: dict[str, Any]) -> dict[str, Any]:
    fetch = status.get("fetch") if isinstance(status.get("fetch"), dict) else {}
    remote = status.get("remote") if isinstance(status.get("remote"), dict) else {}
    if not status.get("available") or not status.get("update_supported"):
        ok, code, phase, title = False, "plugin_update_source_unsupported", "repository_status", "当前安装源不支持自动更新"
        retryable = False
    elif fetch.get("attempted") and fetch.get("ok") is False:
        ok, code, phase, title = False, "plugin_update_fetch_failed", "remote_fetch", "远端更新检查未完成"
        retryable = True
    elif (remote or {}).get("error"):
        ok, code, phase, title = False, "plugin_update_remote_unavailable", "revision_compare", "无法确认远端版本"
        retryable = False
    elif status.get("update_available") and status.get("dirty"):
        ok, code, phase, title = True, "plugin_update_available_dirty", "update_ready", "发现更新，但 worktree 阻止自动应用"
        retryable = False
    elif status.get("update_available"):
        ok, code, phase, title = True, "plugin_update_available", "update_ready", "发现可应用的插件更新"
        retryable = False
    elif int(status.get("ahead") or 0) > 0:
        ok, code, phase, title = True, "plugin_update_local_ahead", "repository_status", "本地版本领先 upstream"
        retryable = False
    elif status.get("dirty"):
        ok, code, phase, title = True, "plugin_update_worktree_dirty", "repository_status", "版本已检查，worktree 有未提交改动"
        retryable = False
    else:
        ok, code, phase, title = True, "plugin_update_current", "repository_status", "插件已经是最新版本"
        retryable = False
    source = status.get("source") if isinstance(status.get("source"), dict) else {}
    return diagnostic(
        ok=ok,
        code=code,
        phase=phase,
        title=title,
        message=str(status.get("message") or title),
        details=(
            detail("Repository", source.get("remote_url") or source.get("remote_name") or "未识别"),
            detail("Local SHA", _short_sha(status) or "unknown"),
            detail("Remote SHA", _short_sha(status, "remote") or "unknown"),
            detail("Worktree", "dirty" if status.get("dirty") else "clean", "warn" if status.get("dirty") else "ok"),
        ),
        steps=(
            _repository_step(status, key="repository_status", label="Read repository status"),
            *_remote_steps(status),
            _compare_step(status),
        ),
        suggestion=(
            "先处理 worktree 的未提交改动，再应用更新。"
            if status.get("dirty")
            else "检查网络、remote 与 upstream 配置后再重试。"
            if not ok
            else "确认待更新提交后，可执行 fast-forward update。"
            if status.get("update_available")
            else "无需操作。"
        ),
        retryable=retryable,
        partial=bool(not ok and status.get("available")),
        outcome_unknown=False,
    )


def _is_timeout_result(pull: dict[str, Any]) -> bool:
    text = f"{pull.get('error') or ''} {pull.get('output') or ''}".lower()
    return any(marker in text for marker in ("timeout", "timed out", "超时"))


def _update_diagnostic(result: dict[str, Any]) -> dict[str, Any]:
    manager_ok = bool(result.get("ok"))
    updated = bool(result.get("updated"))
    before = result.get("before") if isinstance(result.get("before"), dict) else result.get("status")
    before = before if isinstance(before, dict) else {}
    after = result.get("status") if isinstance(result.get("status"), dict) else {}
    pull = result.get("pull") if isinstance(result.get("pull"), dict) else {}
    fetch = before.get("fetch") if isinstance(before.get("fetch"), dict) else {}
    outcome_unknown = bool(pull and pull.get("ok") is False and _is_timeout_result(pull))
    if manager_ok and updated:
        ok, code, phase, title = True, "plugin_update_applied", "update_complete", "插件已完成 fast-forward update"
        retryable = False
    elif manager_ok and fetch.get("attempted") and fetch.get("ok") is False:
        ok, code, phase, title = False, "plugin_update_check_incomplete", "remote_fetch", "未能确认是否存在可应用的更新"
        retryable = True
    elif manager_ok:
        ok, code, phase, title = True, "plugin_update_noop", "update_complete", "插件无需更新"
        retryable = False
    elif before.get("dirty"):
        ok, code, phase, title = False, "plugin_update_dirty_worktree", "preflight", "worktree 有未提交改动，更新已拒绝"
        retryable = False
    elif not before.get("update_supported", True):
        ok, code, phase, title = False, "plugin_update_source_unsupported", "preflight", "当前安装源不支持自动更新"
        retryable = False
    elif pull:
        ok = False
        code = "plugin_update_pull_outcome_unknown" if outcome_unknown else "plugin_update_pull_failed"
        phase = "fast_forward_pull"
        title = "Pull 结果未知，请先核对 repository" if outcome_unknown else "Fast-forward pull 未完成"
        retryable = False if outcome_unknown else True
    else:
        ok, code, phase, title = False, "plugin_update_preflight_failed", "preflight", "插件更新前置检查未通过"
        retryable = False

    pull_details = (
        detail("选用 endpoint", _safe_endpoint(pull.get("used_mirror")) or "default remote"),
        detail("Probe 数量", len(pull.get("probes") or [])),
    )
    if not pull:
        pull_step = step("fast_forward_pull", "Run git pull --ff-only", "skipped", "前置检查未要求执行 pull。")
    elif pull.get("ok"):
        pull_step = step("fast_forward_pull", "Run git pull --ff-only", "ok", "Git 已明确返回 fast-forward 成功。", details=pull_details)
    else:
        pull_step = step(
            "fast_forward_pull",
            "Run git pull --ff-only",
            "unknown" if outcome_unknown else "error",
            "Git command 超时，repository 最终状态尚未确认。" if outcome_unknown else "Git 明确返回 pull 失败。",
            details=pull_details,
        )
    if updated:
        verify_status, verify_message = "ok", "已重新读取更新后的 local HEAD。"
    elif outcome_unknown:
        verify_status, verify_message = "unknown", "必须刷新 repository status 后确认 local HEAD。"
    else:
        verify_status, verify_message = "skipped", "没有需要验证的版本变化。"
    before_sha = _short_sha(before) or "unknown"
    after_sha = _short_sha(after) or before_sha
    message = str(result.get("message") or result.get("error") or title)
    return diagnostic(
        ok=ok,
        code=code,
        phase=phase,
        title=title,
        message=message,
        details=(
            detail("更新前 SHA", before_sha),
            detail("更新后 SHA", after_sha, "warn" if outcome_unknown else "ok" if updated else "info"),
            detail(
                "更新结果",
                "updated" if updated else "unchanged" if ok else "check_incomplete" if manager_ok else "failed",
                "ok" if ok else "warn" if manager_ok else "error",
            ),
        ),
        steps=(
            _repository_step(before, key="preflight", label="Validate repository preflight"),
            *_remote_steps(before),
            pull_step,
            step(
                "verify_head",
                "Verify local HEAD",
                verify_status,
                verify_message,
                details=(detail("Before / after SHA", f"{before_sha} / {after_sha}"),),
            ),
        ),
        warnings=("Pull 可能已经修改 local HEAD；确认 repository status 前不要再次更新。",) if outcome_unknown else (),
        suggestion=(
            "先刷新 repository status 并人工核对 local HEAD，禁止直接重试。"
            if outcome_unknown
            else "重启 Bot 后才会加载更新后的代码。"
            if updated
            else "根据失败阶段处理后再重试。"
            if not ok
            else "无需操作。"
        ),
        retryable=retryable,
        partial=bool(code == "plugin_update_check_incomplete"),
        outcome_unknown=outcome_unknown,
    )


def _attach_diagnostic(result: dict[str, Any], operation_diagnostic: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result)
    payload["diagnostic"] = operation_diagnostic
    for field in _DIAGNOSTIC_FIELDS:
        payload.setdefault(field, operation_diagnostic[field])
    return payload


def _exception_detail(exc: BaseException, *, operation: str, operation_id: str = "") -> dict[str, Any]:
    is_update = operation == "update"
    payload = exception_diagnostic(
        exc,
        phase="update_execution" if is_update else "repository_check",
        title="插件更新异常中断" if is_update else "插件更新检查异常中断",
        message="插件更新流程发生内部异常。" if is_update else "插件更新检查发生内部异常。",
        suggestion=(
            "先刷新 repository status 并确认 local HEAD；确认状态前不要直接重试。"
            if is_update
            else "根据 Trace ID 检查脱敏日志后重试。"
        ),
        retryable=not is_update,
        operation_id=operation_id,
    )
    payload["code"] = "plugin_update_exception" if is_update else "plugin_update_check_exception"
    payload["outcome_unknown"] = is_update
    payload["steps"] = [
        step(
            "update_execution" if is_update else "repository_check",
            "Execute plugin update" if is_update else "Check repository update status",
            "unknown" if is_update else "error",
            "异常发生时未取得可信的最终 repository status。" if is_update else "检查流程异常中断。",
        ).to_dict()
    ]
    return payload


def _audit(
    *,
    action: str,
    admin: AdminIdentity,
    outcome: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    webui_audit_log.record(
        action=action,
        qq=admin.qq,
        device_id=admin.device_id,
        target=target,
        detail=detail or {},
        outcome=outcome,
    )


def build_plugin_manager_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/plugin-manager", tags=["plugin-manager"])

    @router.get("/status")
    async def status(
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return _sanitize_manager_value(
            await plugin_update_manager.get_plugin_update_status(
                plugin_config=getattr(runtime, "plugin_config", None),
                refresh=refresh,
            )
        )

    @router.get("/history")
    async def history(
        limit: int = Query(default=30, ge=1, le=100),
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        return _sanitize_manager_value(
            await plugin_update_manager.get_plugin_update_history(
                plugin_config=getattr(runtime, "plugin_config", None),
                limit=limit,
                refresh=refresh,
            )
        )

    @router.post("/check")
    async def check(admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            manager_result = await plugin_update_manager.get_plugin_update_status(
                plugin_config=getattr(runtime, "plugin_config", None),
                refresh=True,
            )
        except Exception as exc:
            failure = _exception_detail(exc, operation="check")
            _audit(
                action="plugin_update_check",
                admin=admin,
                outcome="error",
                detail={"code": failure["code"], "phase": failure["phase"], "trace_id": failure["trace_id"]},
            )
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(f"[webui] 插件更新检查异常：{type(exc).__name__} trace={failure['trace_id']}")
            raise HTTPException(status_code=500, detail=failure) from exc
        result = _sanitize_manager_value(manager_result)
        result = _attach_diagnostic(result, _check_diagnostic(result))
        outcome = "ok" if result["diagnostic"].get("ok") else "error"
        _audit(
            action="plugin_update_check",
            admin=admin,
            outcome=outcome,
            target=str((result.get("source") or {}).get("upstream") or ""),
            detail={
                "source_type": result.get("source_type"),
                "update_available": result.get("update_available"),
                "code": result.get("code"),
                "phase": result.get("phase"),
            },
        )
        return result

    @router.post("/update")
    async def update(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        operation_id = uuid.uuid4().hex
        if str(body.get("confirm", "") or "").strip().lower() != "update":
            raise HTTPException(
                status_code=400,
                detail=diagnostic(
                    ok=False,
                    code="plugin_update_confirmation_required",
                    phase="request_validation",
                    title="缺少插件更新确认",
                    message="请求没有提供有效的 update 确认参数。",
                    suggestion="重新确认更新操作后再提交。",
                    retryable=True,
                ),
            )
        try:
            manager_result = await plugin_update_manager.perform_plugin_update(
                plugin_config=getattr(runtime, "plugin_config", None),
            )
        except Exception as exc:
            failure = _exception_detail(exc, operation="update", operation_id=operation_id)
            _audit(
                action="plugin_update_apply",
                admin=admin,
                outcome="unknown",
                detail={"code": failure["code"], "phase": failure["phase"], "trace_id": failure["trace_id"]},
            )
            logger = getattr(runtime, "logger", None)
            if logger is not None:
                logger.warning(f"[webui] 插件更新异常：{type(exc).__name__} trace={failure['trace_id']}")
            raise HTTPException(status_code=500, detail=failure) from exc
        result = _sanitize_manager_value(manager_result)
        update_report = _update_diagnostic(result)
        update_report["operation_id"] = operation_id
        result = _attach_diagnostic(result, update_report)
        status_body = result.get("status") if isinstance(result.get("status"), dict) else {}
        target = str(((status_body or {}).get("source") or {}).get("upstream") or "")
        _audit(
            action="plugin_update_apply",
            admin=admin,
            outcome="ok" if result.get("ok") else "error",
            target=target,
            detail={
                "updated": result.get("updated"),
                "code": result.get("code"),
                "phase": result.get("phase"),
                "source_type": (status_body or {}).get("source_type"),
            },
        )
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            if result.get("ok"):
                logger.info(f"[webui] 管理员 {admin.qq} 执行插件更新：updated={bool(result.get('updated'))}")
            else:
                logger.warning(
                    f"[webui] 管理员 {admin.qq} 执行插件更新失败："
                    f"code={result.get('code')} phase={result.get('phase')}"
                )
        return result

    return router
