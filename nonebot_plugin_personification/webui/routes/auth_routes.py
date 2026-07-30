from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ...core import admin_acl, notify, webui_audit_log, webui_auth_store
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
        trusted: bool = False,
    ) -> bool:
        """签发设备 token + 写 cookie + 审计 + 通知。返回 pending（是否待审批）。

        trusted=True（免验证设备）直接批准，跳过审批。
        """
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        require_approval = (
            not trusted
            and bool(getattr(runtime.plugin_config, "personification_webui_require_device_approval", False))
            and webui_auth_store.has_any_approved_device()
        )
        device_status = "pending" if require_approval else "approved"
        token = webui_auth_store.issue_device_token(qq, ua, ip, label=device_label, status=device_status)
        webui_auth_store.reset_login_attempts(ip)
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
            target=str(device_label or ""),
            outcome="pending" if require_approval else ("trusted" if trusted else "ok"),
        )
        return require_approval

    @router.post("/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, request: Request, response: Response) -> LoginResponse:
        ip = get_client_ip(request)
        if webui_auth_store.is_login_locked(ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        qq = payload.qq.strip()
        is_admin = qq in runtime.superusers or admin_acl.is_plugin_admin(qq)
        if not is_admin:
            webui_auth_store.record_login_attempt(ip)
            raise HTTPException(status_code=403, detail="该 QQ 非插件管理员")
        ua = get_user_agent(request)
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]

        # 免验证设备：当前浏览器指纹被登记为信任 → 直接登录，跳过验证码/批准
        if webui_auth_store.match_trusted_device(qq, ua):
            pending = _issue_session(
                qq=qq, ua=ua, ip=ip, device_label="免验证设备",
                request=request, response=response, trusted=True,
            )
            webui_audit_log.record(action="login_passwordless", qq=qq, ip_hash=ip_hash, outcome="ok")
            return LoginResponse(sent=True, passwordless=True, pending=pending,
                                 message="免验证设备，已直接登录")

        # 常规：发送验证码 + 创建可在私聊批准的登录请求
        code = webui_auth_store.create_verify_code(qq)
        request_id, approve_code = webui_auth_store.create_login_request(qq, ua, ip, payload.qq.strip())
        message = (
            f"【拟人插件 WebUI】登录请求\n"
            f"验证码：{code}（5 分钟内有效）\n"
            f"或直接回复『同意登录 {approve_code}』批准本次登录，回复『拒绝登录 {approve_code}』拒绝。\n"
            f"来源 IP：{ip or '未知'}；若非本人操作请回复拒绝。"
        )
        sent = await notify.send_to_admin(runtime.get_bots, qq, message)
        if not sent:
            webui_auth_store.record_login_attempt(ip)
            webui_audit_log.record(action="login_code_sent", qq=qq, ip_hash=ip_hash, outcome="bot_unreachable")
            raise HTTPException(status_code=502, detail="无法向该 QQ 发送私聊（Bot 离线或非好友）")
        webui_audit_log.record(action="login_code_sent", qq=qq, ip_hash=ip_hash)
        return LoginResponse(sent=True, request_id=request_id,
                             message="已发送验证码，也可在 QQ 私聊直接回复『同意登录』批准")

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
        ip = get_client_ip(request)
        ua = get_user_agent(request)
        pending = _issue_session(
            qq=str(req.get("qq", "")), ua=ua, ip=ip,
            device_label=str(req.get("label", "")), request=request, response=response,
        )
        return {"status": "approved", "success": True, "pending": pending}

    @router.post("/verify", response_model=VerifyResponse)
    async def verify(payload: VerifyRequest, request: Request, response: Response) -> VerifyResponse:
        ip = get_client_ip(request)
        if webui_auth_store.is_login_locked(ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        qq = payload.qq.strip()
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        if not webui_auth_store.consume_verify_code(qq, payload.code):
            webui_auth_store.record_login_attempt(ip)
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
        return VerifyResponse(success=True, message="登录成功")

    @router.get("/me")
    async def me(admin: AdminIdentity = Depends(require_admin)) -> AdminIdentity:
        return admin

    @router.post("/logout")
    async def logout(response: Response, admin: AdminIdentity = Depends(require_admin)) -> dict:
        webui_auth_store.revoke_device(admin.device_id)
        response.delete_cookie(get_cookie_name(), path="/personification")
        response.delete_cookie(_CSRF_COOKIE_NAME, path="/personification")
        return {"success": True}

    @router.get("/eligible-admins")
    async def eligible_admins() -> dict:
        """返回登录页辅助信息。

        默认不向未登录访客公开管理员 QQ；需要旧版下拉体验时显式开启
        personification_webui_expose_admin_list。
        """
        if not bool(getattr(runtime.plugin_config, "personification_webui_expose_admin_list", False)):
            return {"admins": [], "manual_entry": True, "source_hidden": True}

        from ...core import admin_acl as _acl

        seen: set[str] = set()
        out: list[dict[str, str]] = []
        for qq in (runtime.superusers or set()):
            sid = str(qq or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                out.append({"qq": sid, "source": "SUPERUSERS (.env)"})
        try:
            for qq in _acl.load_plugin_admins():
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
        pending_ids = {item["id"] for item in webui_auth_store.list_pending_devices()}
        if device_id not in pending_ids:
            raise HTTPException(status_code=404, detail="待审批设备不存在")
        ok = webui_auth_store.approve_device(device_id)
        webui_audit_log.record(
            action="device_approve",
            qq=admin.qq,
            device_id=admin.device_id,
            target=device_id,
            outcome="ok" if ok else "no_op",
        )
        return {"success": ok}

    @router.delete("/devices/{device_id}")
    async def revoke_device(
        device_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        # 允许撤销同 QQ 的设备，或任一待审批设备（拒绝新设备）
        owned = {item["id"] for item in webui_auth_store.list_devices(admin.qq)}
        pending = {item["id"] for item in webui_auth_store.list_pending_devices()}
        if device_id not in owned and device_id not in pending:
            raise HTTPException(status_code=404, detail="设备不存在或非本账号")
        ok = webui_auth_store.revoke_device(device_id)
        webui_audit_log.record(
            action="device_revoke",
            qq=admin.qq,
            device_id=admin.device_id,
            target=device_id,
            outcome="ok" if ok else "no_op",
        )
        return {"success": ok}

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
        """把某台已登录设备登记为免验证：之后同指纹(UA)从该 QQ 登录跳过验证。"""
        target = next(
            (it for it in webui_auth_store.list_devices(admin.qq) if it.get("id") == device_id),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail="设备不存在或非本账号")
        trust_id = webui_auth_store.add_trusted_device(
            admin.qq, str(target.get("ua", "")), str(target.get("label", "")) or "免验证设备"
        )
        webui_audit_log.record(
            action="device_trust", qq=admin.qq, device_id=admin.device_id, target=device_id, outcome="ok"
        )
        return {"success": True, "trust_id": trust_id}

    @router.delete("/trusted-devices/{trust_id}")
    async def untrust_device(trust_id: str, admin: AdminIdentity = Depends(require_admin)) -> dict:
        owned = {it["id"] for it in webui_auth_store.list_trusted_devices(admin.qq)}
        if trust_id not in owned:
            raise HTTPException(status_code=404, detail="免验证设备不存在或非本账号")
        ok = webui_auth_store.remove_trusted_device(trust_id)
        webui_audit_log.record(
            action="device_untrust", qq=admin.qq, device_id=admin.device_id, target=trust_id,
            outcome="ok" if ok else "no_op",
        )
        return {"success": ok}

    return router
