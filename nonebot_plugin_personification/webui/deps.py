from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException, Request

from ..core import webui_auth_store
from .schemas import AdminIdentity


_COOKIE_NAME = "personification_webui_token"
_CSRF_HEADER_NAME = "x-personification-csrf"
# 非 GET/HEAD/OPTIONS 请求都必须带 CSRF header；
# 浏览器跨站点 form POST 无法设置自定义 header，可阻断常见 CSRF。
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_client_ip(request: Request) -> str:
    """取 X-Forwarded-For 头第一段，否则取 client.host。"""
    forwarded = request.headers.get("x-forwarded-for", "") or ""
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    client = request.client
    return getattr(client, "host", "") or ""


def get_user_agent(request: Request) -> str:
    return request.headers.get("user-agent", "")[:512]


def require_admin(request: Request) -> AdminIdentity:
    """FastAPI Depends：要求合法 device token + CSRF token（非 safe method）。"""
    token = request.cookies.get(_COOKIE_NAME, "")
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    ua = get_user_agent(request)
    record = webui_auth_store.lookup_device(token, ua=ua)
    if not record:
        raise HTTPException(status_code=401, detail="设备令牌无效、已被撤销或已过期")
    # 待审批设备：除"查询自身状态"外一律拒绝，返回可识别的 detail 供前端切到等待页
    if str(record.get("status", "approved") or "approved") == "pending":
        raise HTTPException(status_code=403, detail="DEVICE_PENDING")
    # CSRF: 改变服务器状态的请求必须带匹配的 CSRF token header
    if request.method.upper() not in _CSRF_SAFE_METHODS:
        expected = str(record.get("csrf_token", "") or "")
        provided = request.headers.get(_CSRF_HEADER_NAME, "") or ""
        if not expected or not provided or not _consteq(expected, provided):
            raise HTTPException(status_code=403, detail="CSRF token 校验失败")

    return AdminIdentity(
        qq=str(record.get("qq", "")),
        device_id=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        label=str(record.get("label", "")),
    )


def _consteq(a: str, b: str) -> bool:
    """常数时间字符串比较，防 timing attack。"""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode("utf-8"), b.encode("utf-8")):
        result |= x ^ y
    return result == 0


def get_cookie_name() -> str:
    return _COOKIE_NAME


def get_csrf_header_name() -> str:
    return _CSRF_HEADER_NAME
