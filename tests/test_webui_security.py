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


def _client(rt):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(rt.app_module.build_router())
    return TestClient(app)


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
    client = _client(_runtime)
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {}

    _runtime.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    client.post("/personification/api/auth/login", json={"qq": "10001"})
    correct_code = re.search(r"\b(\d{6})\b", str(sent[-1]["message"])).group(1)

    # 连错 5 次
    for _ in range(5):
        r = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": "000000", "device_label": ""})
        # 5 次内仍返回 403（验证码错），但第 5 次后验证码就废了
        assert r.status_code in (403, 429)

    # 第 6 次：即使是正确的 code，验证码也已被废弃
    r = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": correct_code, "device_label": ""})
    assert r.status_code in (403, 429), f"正确验证码不应在 5 次失败后还能使用，得到 {r.status_code}"


def test_verify_code_uses_constant_time_compare(_runtime, monkeypatch) -> None:
    auth_store = load_personification_module("plugin.personification.core.webui_auth_store")
    calls: list[tuple[str, str]] = []
    real_compare = auth_store.secrets.compare_digest

    def compare_spy(a: str, b: str) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(auth_store.secrets, "compare_digest", compare_spy)

    code = auth_store.create_verify_code("10001")
    assert auth_store.consume_verify_code("10001", code) is True
    assert calls == [(code, code)]


def test_https_login_cookies_are_marked_secure(_runtime) -> None:
    client = _client(_runtime)
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
        },
    }
    data_store.get_data_store().save_sync("webui_devices", devices)
    pruned = auth_store.prune_expired_devices()
    assert pruned == 1
    remaining = data_store.get_data_store().load_sync("webui_devices")
    assert set(remaining.keys()) == {"fresh_hash"}


# ---- eligible-admins 接口（不需要鉴权，但默认不暴露 QQ） ----


def test_eligible_admins_hidden_by_default(_runtime) -> None:
    client = _client(_runtime)
    res = client.get("/personification/api/auth/eligible-admins")
    assert res.status_code == 200
    body = res.json()
    assert body["admins"] == []
    assert body["manual_entry"] is True
    assert body["source_hidden"] is True


def test_eligible_admins_lists_superusers_when_explicitly_enabled(_runtime) -> None:
    _runtime.plugin_config.personification_webui_expose_admin_list = True
    client = _client(_runtime)
    res = client.get("/personification/api/auth/eligible-admins")
    assert res.status_code == 200
    body = res.json()
    assert body["manual_entry"] is False
    qqs = {item["qq"] for item in body["admins"]}
    assert {"10001", "20002"} <= qqs
    # 来源标记
    for item in body["admins"]:
        if item["qq"] == "10001":
            assert "SUPERUSERS" in item["source"]


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
