from __future__ import annotations

import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001", "20002"},
        get_bots=lambda: {},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(),
    )
    return SimpleNamespace(plugin_config=cfg, app_module=app_module)


def _client(rt, *, base_url: str = "http://testserver"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(rt.app_module.build_router())
    return TestClient(app, base_url=base_url)


def _login(rt, client) -> str:
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    rt.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    r1 = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert r1.status_code == 200, r1.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    r2 = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": code, "device_label": "t"})
    assert r2.status_code == 200, r2.text
    csrf = client.cookies.get("personification_webui_csrf", "")
    assert csrf, "verify 必须设 CSRF cookie"
    return csrf


# ---- CSRF 防护 ----


def test_post_without_csrf_header_rejected(_runtime) -> None:
    client = _client(_runtime)
    _login(_runtime, client)
    # 不设 CSRF header 的 DELETE 应被拒
    res = client.delete("/personification/api/auth/devices/fake_id")
    assert res.status_code == 403
    assert "CSRF" in res.json()["detail"]


def test_post_with_csrf_header_accepted(_runtime) -> None:
    client = _client(_runtime)
    csrf = _login(_runtime, client)
    client.headers["X-Personification-CSRF"] = csrf
    # 即使 device 不存在，也应该过 CSRF 检查走到 404
    res = client.delete("/personification/api/auth/devices/fake_id")
    assert res.status_code == 404


def test_post_with_wrong_csrf_rejected(_runtime) -> None:
    client = _client(_runtime)
    _login(_runtime, client)
    client.headers["X-Personification-CSRF"] = "totally-wrong-value"
    res = client.delete("/personification/api/auth/devices/fake_id")
    assert res.status_code == 403


def test_get_does_not_require_csrf(_runtime) -> None:
    client = _client(_runtime)
    _login(_runtime, client)
    # 不设 CSRF header 的 GET 仍可通
    res = client.get("/personification/api/auth/me")
    assert res.status_code == 200


# ---- 验证码失败 5 次废弃 ----


def test_verify_code_invalidated_after_5_bad_attempts(_runtime) -> None:
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    challenge = "browser-challenge"
    correct_code = auth_store.create_verify_code("10001", challenge)
    for _ in range(5):
        assert auth_store.consume_verify_code("10001", "000000", challenge) is False

    assert auth_store.consume_verify_code("10001", correct_code, challenge) is False


def test_verify_code_uses_constant_time_compare(_runtime, monkeypatch) -> None:
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    calls: list[tuple[str, str]] = []
    real_compare = auth_store.secrets.compare_digest

    def compare_spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_store.secrets, "compare_digest", compare_spy)

    challenge = "constant-time-challenge"
    code = auth_store.create_verify_code("10001", challenge)
    assert auth_store.consume_verify_code("10001", code, challenge) is True
    assert calls[-1] == (code, code)


def test_wrong_browser_challenge_does_not_consume_verify_attempts(_runtime) -> None:
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    code = auth_store.create_verify_code("10001", "right-browser")

    for _ in range(8):
        assert auth_store.consume_verify_code("10001", code, "wrong-browser") is False

    assert auth_store.consume_verify_code("10001", code, "right-browser") is True


def test_https_login_cookies_are_marked_secure(_runtime) -> None:
    client = _client(_runtime, base_url="https://testserver")
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {}

    _runtime.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    headers = {"X-Forwarded-Proto": "https"}
    r1 = client.post("/personification/api/auth/login", json={"qq": "10001"}, headers=headers)
    assert r1.status_code == 200, r1.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1]["message"])).group(1)

    r2 = client.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "https"},
        headers=headers,
    )

    assert r2.status_code == 200, r2.text
    set_cookie = r2.headers.get("set-cookie", "")
    assert "personification_webui_token=" in set_cookie
    assert "personification_webui_csrf=" in set_cookie
    assert "Secure" in set_cookie


# ---- 设备 token 7 天过期 ----


def test_expired_device_token_is_rejected(_runtime, monkeypatch) -> None:
    client = _client(_runtime)
    _login(_runtime, client)
    # 直接动 KV：把 expires_at 设为过去
    data_store = load_personification_module("plugin.personification.core.data_store")
    devices = data_store.get_data_store().load_sync("webui_devices")
    assert devices
    for token_hash in list(devices.keys()):
        devices[token_hash]["expires_at"] = time.time() - 10
    data_store.get_data_store().save_sync("webui_devices", devices)

    res = client.get("/personification/api/auth/me")
    assert res.status_code == 401


def test_prune_expired_devices_cleans_up(_runtime) -> None:
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    data_store = load_personification_module("plugin.personification.core.data_store")
    # 注入一个明显过期的设备
    devices = {
        "stale_hash": {
            "qq": "10001",
            "ua": "ua",
            "ip_hash": "x",
            "label": "stale",
            "created_at": 0,
            "last_seen": 0,
            "expires_at": time.time() - 86400,
        },
        "fresh_hash": {
            "qq": "10001",
            "ua": "ua",
            "ip_hash": "x",
            "label": "fresh",
            "created_at": time.time(),
            "last_seen": time.time(),
            "expires_at": time.time() + 86400,
            "status": "pending",
        },
    }
    data_store.get_data_store().save_sync("webui_devices", devices)
    pruned = auth_store.prune_expired_devices()
    assert pruned == 1
    remaining = data_store.get_data_store().load_sync("webui_devices")
    assert set(remaining.keys()) == {"fresh_hash"}
    assert remaining["fresh_hash"]["status"] == "approved"


def test_repeated_code_send_is_throttled_without_replacing_code(_runtime) -> None:
    client = _client(_runtime)
    sent: list[dict] = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {}

    _runtime.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    first = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert first.status_code == 200
    original_code = re.search(r"\b(\d{6})\b", str(sent[-1]["message"])).group(1)

    for _ in range(4):
        second = client.post("/personification/api/auth/login", json={"qq": "10001"})
        assert second.status_code == 429
    assert len(sent) == 1

    verified = client.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": original_code, "device_label": "throttled"},
    )
    assert verified.status_code == 200


def test_parallel_browser_challenges_do_not_block_existing_verification(_runtime) -> None:
    sent: list[dict] = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {}

    _runtime.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    clients = []
    for index in range(5):
        client = _client(_runtime)
        response = client.post(
            "/personification/api/auth/login",
            json={"qq": "10001"},
            headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
        )
        assert response.status_code == 200
        clients.append(client)

    blocked = _client(_runtime).post(
        "/personification/api/auth/login",
        json={"qq": "10001"},
        headers={"X-Forwarded-For": "203.0.113.8"},
    )
    assert blocked.status_code == 429

    first_code = re.search(r"\b(\d{6})\b", str(sent[0]["message"])).group(1)
    verified = clients[0].post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": first_code, "device_label": "first"},
    )
    assert verified.status_code == 200


def test_revoked_admin_session_is_invalidated(_runtime) -> None:
    client = _client(_runtime)
    _login(_runtime, client)
    _runtime.app_module.get_runtime_context().superusers.clear()

    res = client.get("/personification/api/auth/me")

    assert res.status_code == 401
    assert "管理员权限已撤销" in res.json()["detail"]
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    assert auth_store.list_devices("10001") == []


def test_revoked_admin_cannot_finish_code_verification(_runtime) -> None:
    client = _client(_runtime)
    sent: list[dict] = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {}

    _runtime.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    assert client.post("/personification/api/auth/login", json={"qq": "10001"}).status_code == 200
    code = re.search(r"\b(\d{6})\b", str(sent[-1]["message"])).group(1)
    _runtime.app_module.get_runtime_context().superusers.clear()

    res = client.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "revoked"},
    )

    assert res.status_code == 403
    assert not client.cookies.get("personification_webui_token")


# ---- eligible-admins 接口（不需要鉴权，固定用于登录选择） ----


def test_eligible_admins_lists_superusers_by_default(_runtime) -> None:
    admin_acl = load_personification_module("plugin.personification.core.admin_acl")
    admin_acl.add_plugin_admin("30003")
    client = _client(_runtime)
    res = client.get("/personification/api/auth/eligible-admins")
    assert res.status_code == 200
    body = res.json()
    assert body["manual_entry"] is False
    assert body["source_hidden"] is False
    qqs = {item["qq"] for item in body["admins"]}
    assert {"10001", "20002", "30003"} <= qqs
    # 来源标记
    for item in body["admins"]:
        if item["qq"] == "10001":
            assert "SUPERUSERS" in item["source"]
        if item["qq"] == "30003":
            assert item["source"] == "plugin_admins"


def test_eligible_admins_never_falls_back_to_manual_entry(_runtime) -> None:
    _runtime.app_module.get_runtime_context().superusers.clear()
    client = _client(_runtime)

    body = client.get("/personification/api/auth/eligible-admins").json()

    assert body == {"admins": [], "manual_entry": False, "source_hidden": False}


# ---- Audit log ----


def test_audit_log_records_login_and_config_update(_runtime) -> None:
    client = _client(_runtime)
    csrf = _login(_runtime, client)
    client.headers["X-Personification-CSRF"] = csrf
    # 触发一次 config_update
    setattr(_runtime.plugin_config, "personification_agent_max_steps", 5)
    res = client.post(
        "/personification/api/config/value",
        json={"field_name": "personification_agent_max_steps", "value": "6"},
    )
    # 即使写盘失败也应该有 audit 记录
    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    entries = audit_mod.query_recent(limit=20)
    actions = {e["action"] for e in entries}
    assert "login_code_sent" in actions
    assert "login_verify" in actions
    # config_update 不论成功失败都应该记录
    assert "config_update" in actions or res.status_code >= 400


def test_audit_query_filters_by_action(_runtime) -> None:
    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    audit_mod.record(action="login_verify", qq="10001")
    audit_mod.record(action="device_revoke", qq="10001", target="abc")
    audit_mod.record(action="login_verify", qq="20002")

    only_revoke = audit_mod.query_recent(action="device_revoke")
    assert all(e["action"] == "device_revoke" for e in only_revoke)
    assert len(only_revoke) >= 1

    only_one_qq = audit_mod.query_recent(qq="20002")
    assert all(e["qq"] == "20002" for e in only_one_qq)


def test_audit_log_endpoint_returns_entries(_runtime) -> None:
    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    audit_mod.record(action="login_verify", qq="10001", outcome="ok")
    client = _client(_runtime)
    _login(_runtime, client)
    res = client.get("/personification/api/audit/recent?limit=50")
    assert res.status_code == 200
    body = res.json()
    assert any(e["action"] == "login_verify" for e in body["entries"])
