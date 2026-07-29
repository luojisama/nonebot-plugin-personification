from __future__ import annotations

import re

from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def _set_csrf(client) -> None:
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_login_sends_code_without_chat_approval_request(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["request_id"] == ""
    assert client.cookies.get("personification_webui_login_challenge")
    msg = str(_runtime_context.sent[-1].get("message", ""))
    assert re.search(r"\b(\d{6})\b", msg)
    assert "同意登录" not in msg


def test_code_is_bound_to_originating_browser_challenge(_runtime_context) -> None:
    original = _build_client(_runtime_context)
    assert original.post("/personification/api/auth/login", json={"qq": "10001"}).status_code == 200
    code = re.search(r"\b(\d{6})\b", str(_runtime_context.sent[-1].get("message", ""))).group(1)

    other = _build_client(_runtime_context)
    sent_before = len(_runtime_context.sent)
    assert other.post("/personification/api/auth/login", json={"qq": "10001"}).status_code == 200
    assert len(_runtime_context.sent) == sent_before + 1
    rejected = other.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "other"},
    )
    assert rejected.status_code == 403
    assert not other.cookies.get("personification_webui_token")

    accepted = original.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "original"},
    )
    assert accepted.status_code == 200
    assert original.cookies.get("personification_webui_token")


def test_legacy_trusted_device_registration_is_disabled(_runtime_context) -> None:
    # 先正常登录，拿到会话与固定 UA
    client = _build_client(_runtime_context)
    client.headers["user-agent"] = "TrustUA/1.0"
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    # 找到当前设备并设为免验证
    devs = client.get("/personification/api/auth/devices").json()["devices"]
    did = devs[0]["id"]
    disabled = client.post(f"/personification/api/auth/devices/{did}/trust")
    assert disabled.status_code == 410
    assert client.get("/personification/api/auth/trusted-devices").json()["devices"] == []

    # 新客户端即使 UA 相同也必须重新向管理员发送验证码，不能依赖可伪造 UA 免验证
    sent_before = len(_runtime_context.sent)
    fresh = _build_client(_runtime_context)
    fresh.headers["user-agent"] = "TrustUA/1.0"
    r = fresh.post("/personification/api/auth/login", json={"qq": "10001"}).json()
    assert r["passwordless"] is False
    assert len(_runtime_context.sent) == sent_before + 1
    assert not fresh.cookies.get("personification_webui_token")
    assert fresh.get("/personification/api/auth/me").status_code == 401


def test_non_admin_login_rejected_still(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    assert client.post("/personification/api/auth/login", json={"qq": "99999"}).status_code == 403
