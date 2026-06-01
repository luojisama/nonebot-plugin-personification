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
    VerifyRequest,
    VerifyResponse,
)


_CSRF_COOKIE_NAME = "personification_webui_csrf"


def build_auth_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/auth", tags=["auth"])

    @router.post("/login", response_model=LoginResponse)
    async def login(payload: LoginRequest, request: Request) -> LoginResponse:
        ip = get_client_ip(request)
        if webui_auth_store.is_login_locked(ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        qq = payload.qq.strip()
        is_admin = (
            qq in runtime.superusers
            or admin_acl.is_plugin_admin(qq)
        )
        if not is_admin:
            webui_auth_store.record_login_attempt(ip)
            raise HTTPException(status_code=403, detail="该 QQ 非插件管理员")
        code = webui_auth_store.create_verify_code(qq)
        message = (
            f"【拟人插件 WebUI】登录验证码：{code}\n"
            "5 分钟内有效，请勿向他人透露。\n"
            f"若非本人操作，请尽快撤销设备或联系管理员。来源 IP：{ip or '未知'}"
        )
        sent = await notify.send_to_admin(runtime.get_bots, qq, message)
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        if not sent:
            webui_auth_store.record_login_attempt(ip)
            webui_audit_log.record(
                action="login_code_sent",
                qq=qq,
                ip_hash=ip_hash,
                outcome="bot_unreachable",
            )
            raise HTTPException(status_code=502, detail="无法向该 QQ 发送私聊（Bot 离线或非好友）")
        webui_audit_log.record(action="login_code_sent", qq=qq, ip_hash=ip_hash)
        return LoginResponse(sent=True, message="验证码已发送至 QQ 私聊")

    @router.post("/verify", response_model=VerifyResponse)
    async def verify(payload: VerifyRequest, request: Request, response: Response) -> VerifyResponse:
        ip = get_client_ip(request)
        if webui_auth_store.is_login_locked(ip):
            raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
        qq = payload.qq.strip()
        ip_hash = hashlib.sha256((ip or "").encode("utf-8")).hexdigest()[:16]
        if not webui_auth_store.consume_verify_code(qq, payload.code):
            webui_auth_store.record_login_attempt(ip)
            webui_audit_log.record(
                action="login_verify",
                qq=qq,
                ip_hash=ip_hash,
                outcome="bad_code",
            )
            raise HTTPException(status_code=403, detail="验证码错误或已过期")
        ua = get_user_agent(request)
        token = webui_auth_store.issue_device_token(qq, ua, ip, label=payload.device_label)
        webui_auth_store.reset_login_attempts(ip)
        # 找回刚生成的设备记录拿 csrf_token
        record = webui_auth_store.lookup_device(token, ua=ua) or {}
        csrf_token = str(record.get("csrf_token", "") or "")
        cookie_max_age = 60 * 60 * 24 * 7  # 与 _DEVICE_TOKEN_TTL_SECONDS 对齐
        response.set_cookie(
            key=get_cookie_name(),
            value=token,
            max_age=cookie_max_age,
            httponly=True,
            samesite="lax",
            path="/personification",
        )
        if csrf_token:
            # 不 HttpOnly：前端 JS 读后放到 X-Personification-CSRF header（double-submit cookie）
            response.set_cookie(
                key=_CSRF_COOKIE_NAME,
                value=csrf_token,
                max_age=cookie_max_age,
                httponly=False,
                samesite="lax",
                path="/personification",
            )
        webui_audit_log.record(
            action="login_verify",
            qq=qq,
            ip_hash=ip_hash,
            device_id=hashlib.sha256(token.encode("utf-8")).hexdigest(),
            target=str(payload.device_label or ""),
            outcome="ok",
        )
        # 告知 SUPERUSER：新设备登录
        try:
            superusers = list(runtime.superusers or [])
            await notify.startup_notify_admins(
                get_bots=runtime.get_bots,
                superusers=superusers,
                plugin_admins=[],
                message=(
                    f"【拟人 WebUI】新设备登录\n"
                    f"QQ：{qq}\n"
                    f"设备：{payload.device_label or '未命名'}\n"
                    f"IP：{ip or '未知'}"
                ),
            )
        except Exception:
            pass
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
        """返回可登录的 QQ 列表（SUPERUSERS + plugin_admins 去重）。
        无鉴权——展示在登录页，不暴露敏感信息。
        """
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
        return {"admins": out}

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
            )
            for item in raw
        ]
        return DeviceListResponse(devices=items, current_device_id=admin.device_id)

    @router.delete("/devices/{device_id}")
    async def revoke_device(
        device_id: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        # 仅允许撤销同 QQ 的设备
        owned = {item["id"] for item in webui_auth_store.list_devices(admin.qq)}
        if device_id not in owned:
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

    return router
