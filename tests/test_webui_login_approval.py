from __future__ import annotations

import re

from ._loader import load_personification_module

from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401

webui_auth_store = load_personification_module("plugin.personification.core.webui_auth_store")


def _set_csrf(client) -> None:
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_login_creates_approvable_request(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["request_id"]
    # 私聊批准消息应含 4 位批准码
    msg = str(_runtime_context.sent[-1].get("message", ""))
    assert "同意登录" in msg
    assert re.search(r"同意登录\s*(\d{4})", msg)


def test_chat_approval_completes_login(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    rid = client.post("/personification/api/auth/login", json={"qq": "10001"}).json()["request_id"]
    # 轮询：尚未批准
    s1 = client.get("/personification/api/auth/login-status", params={"request_id": rid}).json()
    assert s1["status"] == "pending"
    # 模拟管理员私聊批准（直接调 store，等价于 bot 命令处理）
    assert webui_auth_store.approve_login_request("10001") is not None
    # 再轮询：完成发证
    s2 = client.get("/personification/api/auth/login-status", params={"request_id": rid}).json()
    assert s2["status"] == "approved" and s2["success"] is True
    assert client.cookies.get("personification_webui_token")
    assert client.get("/personification/api/auth/me").status_code == 200


def test_chat_denial_blocks_login(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    rid = client.post("/personification/api/auth/login", json={"qq": "10001"}).json()["request_id"]
    assert webui_auth_store.deny_login_request("10001") is not None
    s = client.get("/personification/api/auth/login-status", params={"request_id": rid}).json()
    assert s["status"] == "denied"
    assert "success" not in s


def test_trusted_device_enables_passwordless_login(_runtime_context) -> None:
    # 先正常登录，拿到会话与固定 UA
    client = _build_client(_runtime_context)
    client.headers["user-agent"] = "TrustUA/1.0"
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    # 找到当前设备并设为免验证
    devs = client.get("/personification/api/auth/devices").json()["devices"]
    did = devs[0]["id"]
    assert client.post(f"/personification/api/auth/devices/{did}/trust").status_code == 200
    assert len(client.get("/personification/api/auth/trusted-devices").json()["devices"]) == 1

    # 新客户端（同 UA）直接 /login → 免验证登录
    fresh = _build_client(_runtime_context)
    fresh.headers["user-agent"] = "TrustUA/1.0"
    r = fresh.post("/personification/api/auth/login", json={"qq": "10001"}).json()
    assert r["passwordless"] is True
    assert fresh.cookies.get("personification_webui_token")
    assert fresh.get("/personification/api/auth/me").status_code == 200


def test_non_admin_login_rejected_still(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    assert client.post("/personification/api/auth/login", json={"qq": "99999"}).status_code == 403
