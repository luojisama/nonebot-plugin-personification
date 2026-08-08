from __future__ import annotations

import hashlib
import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...core import admin_acl, notify, webui_audit_log, webui_auth_store
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ..deps import (
    AdminIdentity,
    get_client_ip,
    get_cookie_name,
    get_user_agent,
    require_admin,
)
from ..schemas import (
    DeviceInfo,
    DeviceListResponse,
    LoginRequest,
    LoginResponse,
    PendingDeviceListResponse,
    VerifyRequest,
    VerifyResponse,
)


_CSRF_COOKIE_NAME = "personification_webui_csrf"
_LOGIN_CHALLENGE_COOKIE_NAME = "personification_webui_login_challenge"


def _record_device_audit(runtime: Any, **kwargs: Any) -> bool:
    try:
        webui_audit_log.record(**kwargs)
        return True
    except Exception as exc:
        logger = getattr(runtime, "logger", None)
        if logger is not None:
            logger.warning(
                f"[device operation] audit_failed action={kwargs.get('action', '')} "
                f"exception={type(exc).__name__}"
            )
        return False


def _device_result(
    payload: dict[str, Any],
    *,
    changed: bool,
    code: str,
    no_op_code: str,
    title: str,
    no_op_title: str,
    message: str,
    no_op_message: str,
    target: str,
    persist_label: str,
    audit_ok: bool,
) -> dict[str, Any]:
    report = diagnostic(
        ok=changed,
        code=code if changed else no_op_code,
        phase="operation_complete",
        title=title if changed else no_op_title,
        message=message if changed else no_op_message,
        details=(detail("目标设备", target), detail("持久化变更", changed, "ok" if changed else "warn")),
        steps=(
            step("persist", persist_label, "ok" if changed else "warn", message if changed else no_op_message),
            step(
                "audit",
                "记录管理员操作",
                "ok" if audit_ok else "warn",
                "审计记录已保存。" if audit_ok else "持久化结果已确认，但审计记录写入失败。",
            ),
        ),
        warnings=() if audit_ok else ("设备状态结果已确认，但本次管理员审计记录未能写入。",),
        suggestion="刷新设备列表确认当前状态。" if not changed else "",
        retryable=False,
        partial=bool(changed and not audit_ok),
        outcome_unknown=False,
    )
    return {**payload, **report}


def _raise_device_failure(
    runtime: Any,
    exc: BaseException,
    *,
    code: str,
    title: str,
    message: str,
    target: str,
    persist_label: str,
    persistence_started: bool,
) -> None:
    report = exception_diagnostic(
        exc,
        phase="persistence" if persistence_started else "precondition",
        title=title,
        message=message,
        suggestion=(
            "先刷新设备列表确认当前状态；状态未生效时再重试。"
            if persistence_started
            else "刷新设备列表后重试。"
        ),
        retryable=not isinstance(exc, (ValueError, PermissionError)),
    )
    report["code"] = code
    report["details"] = [detail("目标设备", target).to_dict(), *report.get("details", [])]
    report["steps"] = [
        step(
            "persist",
            persist_label,
            "unknown" if persistence_started else "skipped",
            "写入过程异常，最终状态需要重新读取确认。" if persistence_started else "读取操作前状态失败，未开始写入。",
        ).to_dict(),
        step("audit", "记录管理员操作", "skipped", "设备状态未得到明确结果。" if persistence_started else "未执行写操作。").to_dict(),
    ]
    report["partial"] = bool(persistence_started)
    report["outcome_unknown"] = bool(persistence_started)
    logger = getattr(runtime, "logger", None)
    if logger is not None:
        logger.warning(
            f"[device operation] code={code} exception={type(exc).__name__} "
            f"trace={report.get('trace_id', '')}"
        )
    raise HTTPException(status_code=500, detail=report) from exc


def _raise_device_not_found(*, code: str, title: str, message: str, target: str, persist_label: str) -> None:
    report = diagnostic(
        ok=False,
        code=code,
        phase="precondition",
        title=title,
        message=message,
        details=(detail("目标设备", target),),
        steps=(
            step("precondition", "确认设备归属与状态", "error", message),
            step("persist", persist_label, "skipped", "未执行任何写入。"),
            step("audit", "记录管理员操作", "skipped", "未执行写操作。"),
        ),
        suggestion="刷新设备列表后重新选择目标设备。",
        retryable=False,
        partial=False,
        outcome_unknown=False,
    )
    raise HTTPException(status_code=404, detail=report)


def _request_uses_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "") or ""
    first_proto = forwarded_proto.split(",", 1)[0].strip().lower()
    return first_proto == "https" or request.url.scheme == "https"


def build_auth_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    def _issue_session(
        *,
        qq: str,
        ua: str,
        ip: str,
        device_label: str,
        request: Request,
        response: Response,
    ) -> bool:
        """签发已批准的设备 token。管理员验证码本身就是设备授权。"""
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        token = webui_auth_store.issue_device_token(qq, ua, ip, label=device_label, status="approved")
        webui_auth_store.reset_login_attempts(f"send:{ip}")
        webui_auth_store.reset_login_attempts(f"verify:{ip}")
        record = webui_auth_store.lookup_device(token, ua=ua) or {}
        csrf_token = str(record.get("csrf_token", "") or "")
        cookie_max_age = 60 * 60 * 24 * 7
        cookie_secure = _request_uses_https(request)
        response.set_cookie(
            key=get_cookie_name(), value=token, max_age=cookie_max_age,
            httponly=True, secure=cookie_secure, samesite="lax", path="/personification",
        )
        if csrf_token:
            response.set_cookie(
                key=_CSRF_COOKIE_NAME, value=csrf_token, max_age=cookie_max_age,
                httponly=False, secure=cookie_secure, samesite="lax", path="/personification",
            )
        webui_audit_log.record(
            action="login_verify", qq=qq, ip_hash=ip_hash,
            device_id=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            target=str(device_label or ""), outcome="ok",
        )
        return False

    def _is_current_admin(qq: str) -> bool:
        return qq in runtime.superusers or admin_acl.is_plugin_admin(qq)

    @router.post("/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
        ip = get_client_ip(request)
        qq = payload.qq.strip()
        send_rate_key = f"send:{ip}"
        if webui_auth_store.is_login_locked(send_rate_key):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        if not _is_current_admin(qq):
            webui_auth_store.record_login_attempt(send_rate_key)
            raise HTTPException(status_code=403, detail="该 QQ 非插件管理员")
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]

        # 验证码绑定当前浏览器 challenge，其他匿名请求不能消费或覆盖。
        challenge_token = request.cookies.get(_LOGIN_CHALLENGE_COOKIE_NAME, "") or secrets.token_urlsafe(32)
        try:
            code = webui_auth_store.create_verify_code(qq, challenge_token)
        except webui_auth_store.VerifyCodeCooldownError as exc:
            raise HTTPException(
                status_code=429,
                detail=f"验证码已发送，请 {exc.retry_after} 秒后重试",
            ) from exc
        webui_auth_store.record_login_attempt(send_rate_key)
        message = (
            f"【拟人插件 WebUI】登录请求\n"
            f"验证码：{code}（自首次发送起 5 分钟内有效）\n"
            f"来源 IP：{ip or '未知'}；若非本人操作请忽略。"
        )
        sent = await notify.send_to_admin(runtime.get_bots, qq, message)
        if not sent:
            webui_auth_store.discard_verify_code(qq, challenge_token)
            webui_audit_log.record(action="login_code_sent", qq=qq, ip_hash=ip_hash, outcome="bot_unreachable")
            raise HTTPException(status_code=502, detail="无法向该 QQ 发送私聊（Bot 离线或非好友）")
        response.set_cookie(
            _LOGIN_CHALLENGE_COOKIE_NAME,
            challenge_token,
            max_age=300,
            httponly=True,
            secure=_request_uses_https(request),
            samesite="lax",
            path="/personification",
        )
        webui_audit_log.record(action="login_code_sent", qq=qq, ip_hash=ip_hash)
        return LoginResponse(sent=True, message="已向所选管理员发送验证码，有效期以首次发送时间为准")

    @router.get("/login-status")
    async def login_status(
        request_id: str, request: Request, response: Response
    ) -> dict:
        """网页轮询登录请求状态；管理员在私聊批准后，本接口直接完成发证。"""
        status = webui_auth_store.get_login_request_status(request_id)
        if status != "approved":
            return {"status": status}
        req = webui_auth_store.take_approved_login_request(request_id)
        if not req:
            return {"status": webui_auth_store.get_login_request_status(request_id)}
        qq = str(req.get("qq", "") or "").strip()
        if not _is_current_admin(qq):
            webui_audit_log.record(action="login_verify", qq=qq, outcome="admin_revoked")
            raise HTTPException(status_code=403, detail="管理员权限已撤销")
        ip = get_client_ip(request)
        ua = get_user_agent(request)
        pending = _issue_session(
            qq=qq, ua=ua, ip=ip,
            device_label=str(req.get("label", "")), request=request, response=response,
        )
        return {"status": "approved", "success": True, "pending": pending}

    @router.post("/verify", response_model=VerifyResponse)
    async def verify(payload: VerifyRequest, request: Request, response: Response) -> VerifyResponse:
        ip = get_client_ip(request)
        verify_rate_key = f"verify:{ip}"
        if webui_auth_store.is_login_locked(verify_rate_key):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        qq = payload.qq.strip()
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        if not _is_current_admin(qq):
            webui_audit_log.record(action="login_verify", qq=qq, ip_hash=ip_hash, outcome="admin_revoked")
            raise HTTPException(status_code=403, detail="管理员权限已撤销")
        challenge_token = request.cookies.get(_LOGIN_CHALLENGE_COOKIE_NAME, "")
        if not webui_auth_store.consume_verify_code(qq, payload.code, challenge_token):
            webui_auth_store.record_login_attempt(verify_rate_key)
            webui_audit_log.record(action="login_verify", qq=qq, ip_hash=ip_hash, outcome="bad_code")
            raise HTTPException(status_code=403, detail="验证码错误或已过期")
        ua = get_user_agent(request)
        pending = _issue_session(
            qq=qq, ua=ua, ip=ip, device_label=payload.device_label,
            request=request, response=response,
        )
        try:
            await notify.startup_notify_admins(
                get_bots=runtime.get_bots, superusers=list(runtime.superusers or []), plugin_admins=[],
                message=(
                    (f"【拟人 WebUI】新设备待审批\nQQ：{qq}\n设备：{payload.device_label or '未命名'}\nIP：{ip or '未知'}\n请用已登录设备在「设备」页确认。")
                    if pending else
                    (f"【拟人 WebUI】新设备登录\nQQ：{qq}\n设备：{payload.device_label or '未命名'}\nIP：{ip or '未知'}")
                ),
            )
        except Exception:
            pass
        if pending:
            return VerifyResponse(success=True, pending=True, message="设备已登记，等待管理员确认后方可使用")
        response.delete_cookie(_LOGIN_CHALLENGE_COOKIE_NAME, path="/personification")
        return VerifyResponse(success=True, message="登录成功")

    @router.get("/me")
    async def me(admin: AdminIdentity = Depends(require_admin)) -> AdminIdentity:
        return admin

    @router.post("/logout")
    async def logout(response: Response, admin: AdminIdentity = Depends(require_admin)) -> dict:
        webui_auth_store.revoke_device(admin.device_id)
        response.delete_cookie(get_cookie_name(), path="/personification")
        response.delete_cookie(_CSRF_COOKIE_NAME, path="/personification")
        response.delete_cookie(_LOGIN_CHALLENGE_COOKIE_NAME, path="/personification")
        return {"success": True}

    @router.get("/eligible-admins")
    async def eligible_admins() -> dict:
        """返回登录页可选管理员；登录页不接受自行输入 QQ。"""

        from ...core import admin_acl as _acl

        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for qq in sorted(runtime.superusers or set(), key=str):
            sid = str(qq or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append({"qq": sid, "source": "SUPERUSERS (.env)"})
        try:
            for qq in sorted(_acl.load_plugin_admins(), key=str):
                sid = str(qq or "").strip()
                if sid and sid not in seen:
                    seen.add(sid)
                    out.append({"qq": sid, "source": "plugin_admins"})
        except Exception:
            pass
        return {"admins": out, "manual_entry": False, "source_hidden": False}

    @router.get("/devices", response_model=DeviceListResponse)
    async def devices(admin: AdminIdentity = Depends(require_admin)) -> DeviceListResponse:
        raw = webui_auth_store.list_devices(admin.qq)
        items = [
            DeviceInfo(
                id=str(item.get("id", "")),
                qq=str(item.get("qq", "")),
                label=str(item.get("label", "")),
                ua=str(item.get("ua", "")),
                created_at=float(item.get("created_at", 0) or 0),
                last_seen=float(item.get("last_seen", 0) or 0),
                status=str(item.get("status", "approved") or "approved"),
            )
            for item in raw
        ]
        return DeviceListResponse(devices=items, current_device_id=admin.device_id)

    @router.get("/pending-devices", response_model=PendingDeviceListResponse)
    async def pending_devices(_: AdminIdentity = Depends(require_admin)) -> PendingDeviceListResponse:
        raw = webui_auth_store.list_pending_devices()
        items = [
            DeviceInfo(
                id=str(item.get("id", "")),
                qq=str(item.get("qq", "")),
                label=str(item.get("label", "")),
                ua=str(item.get("ua", "")),
                created_at=float(item.get("created_at", 0) or 0),
                last_seen=float(item.get("last_seen", 0) or 0),
                status="pending",
            )
            for item in raw
        ]
        return PendingDeviceListResponse(devices=items)

    @router.post("/devices/{device_id}/approve")
    async def approve_device(
        device_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            pending_ids = {item["id"] for item in webui_auth_store.list_pending_devices()}
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_approve_precondition_failed", title="设备审批未开始",
                message="服务器未能读取待审批设备状态。", target=device_id,
                persist_label="批准设备", persistence_started=False,
            )
        if device_id not in pending_ids:
            _raise_device_not_found(
                code="device_approve_target_not_found", title="设备无法审批",
                message="待审批设备不存在或状态已经变化。", target=device_id, persist_label="批准设备",
            )
        try:
            ok = webui_auth_store.approve_device(device_id)
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_approve_persist_failed", title="设备审批未完成",
                message="服务器未能确认设备审批状态已保存。", target=device_id,
                persist_label="批准设备", persistence_started=True,
            )
        audit_ok = _record_device_audit(
            runtime,
            action="device_approve",
            qq=admin.qq,
            device_id=admin.device_id,
            target=device_id,
            outcome="ok" if ok else "no_op",
        )
        return _device_result(
            {"success": ok}, changed=ok, code="device_approved", no_op_code="device_approve_noop",
            title="设备已批准", no_op_title="设备审批未产生变更",
            message="设备审批状态已持久化。", no_op_message="设备状态未发生变更，请刷新列表确认。",
            target=device_id, persist_label="批准设备", audit_ok=audit_ok,
        )

    @router.delete("/devices/{device_id}")
    async def revoke_device(
        device_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        # 允许撤销同 QQ 的设备，或任一待审批设备（拒绝新设备）
        try:
            owned = {item["id"] for item in webui_auth_store.list_devices(admin.qq)}
            pending = {item["id"] for item in webui_auth_store.list_pending_devices()}
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_revoke_precondition_failed", title="设备撤销未开始",
                message="服务器未能读取设备归属状态。", target=device_id,
                persist_label="撤销设备", persistence_started=False,
            )
        if device_id not in owned and device_id not in pending:
            _raise_device_not_found(
                code="device_revoke_target_not_found", title="设备无法撤销",
                message="设备不存在或不属于当前账号。", target=device_id, persist_label="撤销设备",
            )
        try:
            ok = webui_auth_store.revoke_device(device_id)
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_revoke_persist_failed", title="设备撤销未完成",
                message="服务器未能确认设备令牌已撤销。", target=device_id,
                persist_label="撤销设备", persistence_started=True,
            )
        audit_ok = _record_device_audit(
            runtime,
            action="device_revoke",
            qq=admin.qq,
            device_id=admin.device_id,
            target=device_id,
            outcome="ok" if ok else "no_op",
        )
        return _device_result(
            {"success": ok}, changed=ok, code="device_revoked", no_op_code="device_revoke_noop",
            title="设备已撤销", no_op_title="设备撤销未产生变更",
            message="设备令牌已从持久化存储移除。", no_op_message="设备令牌未发生变更，请刷新列表确认。",
            target=device_id, persist_label="撤销设备", audit_ok=audit_ok,
        )

    @router.get("/trusted-devices")
    async def trusted_devices(admin: AdminIdentity = Depends(require_admin)) -> dict:
        items = [
            {
                "id": str(it.get("id", "")),
                "label": str(it.get("label", "")),
                "ua": str(it.get("ua", "")),
                "created_at": float(it.get("created_at", 0) or 0),
            }
            for it in webui_auth_store.list_trusted_devices(admin.qq)
        ]
        return {"devices": items}

    @router.post("/devices/{device_id}/trust")
    async def trust_device(device_id: str, admin: AdminIdentity = Depends(require_admin)) -> dict:
        """UA 无法证明浏览器身份，旧免验证登记入口已停用。"""
        raise HTTPException(status_code=410, detail="免验证设备功能已停用；退出或 Session 到期后请重新接收管理员验证码")

    @router.delete("/trusted-devices/{trust_id}")
    async def untrust_device(trust_id: str, admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            owned = {it["id"] for it in webui_auth_store.list_trusted_devices(admin.qq)}
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_untrust_precondition_failed", title="免验证移除未开始",
                message="服务器未能读取免验证设备状态。", target=trust_id,
                persist_label="移除免验证设备", persistence_started=False,
            )
        if trust_id not in owned:
            _raise_device_not_found(
                code="device_untrust_target_not_found", title="免验证设备无法移除",
                message="免验证设备不存在或不属于当前账号。", target=trust_id, persist_label="移除免验证设备",
            )
        try:
            ok = webui_auth_store.remove_trusted_device(trust_id)
        except Exception as exc:
            _raise_device_failure(
                runtime, exc, code="device_untrust_persist_failed", title="免验证移除未完成",
                message="服务器未能确认免验证设备记录已移除。", target=trust_id,
                persist_label="移除免验证设备", persistence_started=True,
            )
        audit_ok = _record_device_audit(
            runtime,
            action="device_untrust", qq=admin.qq, device_id=admin.device_id, target=trust_id,
            outcome="ok" if ok else "no_op",
        )
        return _device_result(
            {"success": ok}, changed=ok, code="device_untrusted", no_op_code="device_untrust_noop",
            title="免验证设备已移除", no_op_title="免验证移除未产生变更",
            message="免验证设备记录已从持久化存储移除。", no_op_message="免验证设备记录未发生变更，请刷新列表确认。",
            target=trust_id, persist_label="移除免验证设备", audit_ok=audit_ok,
        )

    return router
