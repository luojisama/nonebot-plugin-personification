from __future__ import annotations

import base64
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._loader import load_personification_module


def _client(monkeypatch, manager, audit_actions=None):
    route_mod = load_personification_module("plugin.personification.webui.routes.mcp_routes")
    schemas = load_personification_module("plugin.personification.webui.schemas")
    runtime = SimpleNamespace(plugin_config=SimpleNamespace(personification_mcp_registry_sources=[]))
    monkeypatch.setattr(route_mod, "get_mcp_manager", lambda _runtime: manager)
    monkeypatch.setattr(
        route_mod.webui_audit_log,
        "record",
        lambda **kwargs: audit_actions.append(kwargs) if audit_actions is not None else None,
    )
    app = FastAPI()
    app.include_router(route_mod.build_mcp_router(runtime=runtime))
    app.dependency_overrides[route_mod.require_admin] = lambda: schemas.AdminIdentity(
        qq="10001",
        device_id="device-1",
        label="test",
    )
    return TestClient(app)


class _RegistryClient:
    def __init__(self) -> None:
        self.fresh_values: list[bool] = []

    async def detail(self, _source, name: str, *, fresh: bool = False):
        self.fresh_values.append(fresh)
        return {
            "server": {"name": name, "title": "Demo", "status": "active"},
            "packages": [],
            "raw": {"name": name, "packages": []},
        }


class _Manager:
    def __init__(self) -> None:
        self.registry_client = _RegistryClient()
        self.refreshed = 0
        self.current = {
            "installation_id": "install-1",
            "desired_enabled": False,
            "process_state": "stopped",
            "authorized_count": 1,
            "registered_count": 0,
            "effective_count": 0,
            "tools": [{"remote_name": "read_demo", "authorized": True}],
        }

    async def refresh_process_states(self) -> int:
        self.refreshed += 1
        return 0

    def list_public(self):
        return [self.current]

    def public_installation(self, _installation_id: str):
        return self.current

    async def install(self, **_kwargs):
        return {**self.current, "tools": []}

    async def toggle_installation(self, _installation_id: str, enabled: bool):
        self.current["desired_enabled"] = enabled
        self.current["process_state"] = "ready" if enabled else "stopped"
        return self.current

    async def toggle_tool(self, _installation_id: str, _remote_name: str, enabled: bool, **_kwargs):
        self.current["tools"][0]["authorized"] = enabled
        return self.current

    async def reload(self):
        return {
            "running": 0,
            "ready": 1,
            "failed": 1,
            "catalog_added": 1,
            "catalog_updated": 2,
            "catalog_removed": 3,
        }


class _BuiltinManager(_Manager):
    def __init__(self) -> None:
        super().__init__()
        self.auth_requests = []
        self.tool_calls = []
        self.current = {
            "installation_id": "builtin_social_platform_research",
            "desired_enabled": True,
            "process_state": "running",
            "tools": [],
        }

    async def builtin_request(self, method: str, params: dict):
        if method.endswith("/status") and "/auth/" not in method:
            return {
                "schema_version": 1,
                "platforms": {
                    "bilibili": {
                        "state": "disabled",
                        "enabled": False,
                        "config": {"enabled": False, "quality_mode": "balanced"},
                    }
                },
            }
        if method.endswith("/configure"):
            return {"schema_version": 1, "platforms": {params["platform"]: {"state": "login_required"}}}
        if method.endswith("/auth/start"):
            self.auth_requests.append(dict(params))
            return {"session_id": "session-1", "platform": params["platform"], "status": "waiting_scan", "qr_available": True, "expires_at": time.time() + 900, "remaining_seconds": 900, "login_mode": params.get("mode", "embedded_qr")}
        if method.endswith("/auth/status"):
            return {"session_id": params["session_id"], "platform": "bilibili", "status": "waiting_scan", "qr_available": True, "expires_at": time.time() + 300}
        if method.endswith("/auth/qrcode"):
            return {"data_base64": base64.b64encode(b"png-data").decode("ascii")}
        if method.endswith("/auth/logout"):
            return {"platform": params["platform"], "state": "login_required", "authenticated": False}
        raise KeyError(method)

    async def builtin_call_tool(self, remote_name: str, arguments: dict):
        assert remote_name in {"social_content_search", "research_game_slang"}
        self.tool_calls.append((remote_name, dict(arguments)))
        return json.dumps({
            "schema_version": 1,
            "packet_id": "packet-preview",
            "trust": "untrusted_data_only",
            "retrieved_at": time.time(),
            "expires_at": time.time() + 3600,
            "partial": False,
            "platform_statuses": {},
            "items": [],
            "filtered_counts": {},
            "warnings": [],
        })


def test_mcp_api_uses_fresh_registry_fetch_and_strict_bool_requests(monkeypatch) -> None:
    manager = _Manager()
    client = _client(monkeypatch, manager)

    detail_response = client.get("/api/mcp/detail", params={"name": "io.example/demo", "fresh": "true"})
    assert detail_response.status_code == 200
    assert manager.registry_client.fresh_values == [True]

    install_response = client.post(
        "/api/mcp/install",
        json={
            "server_name": "io.example/demo",
            "package_digest": "digest",
            "confirm_execution": True,
        },
    )
    assert install_response.status_code == 200
    assert manager.registry_client.fresh_values[-1] is True

    invalid_toggle = client.post("/api/mcp/installations/install-1/toggle", json={"enabled": "false"})
    assert invalid_toggle.status_code == 400
    assert invalid_toggle.json()["detail"]["code"] == "mcp_request_invalid"

    invalid_tool = client.post(
        "/api/mcp/installations/install-1/tools/read_demo/toggle",
        json={"enabled": True, "confirm_side_effect": "true"},
    )
    assert invalid_tool.status_code == 400
    assert invalid_tool.json()["detail"]["phase"] == "validation"


def test_mcp_api_refreshes_process_state_and_returns_operation_diagnostics(monkeypatch) -> None:
    manager = _Manager()
    client = _client(monkeypatch, manager)

    listed = client.get("/api/mcp/installations")
    assert listed.status_code == 200
    assert manager.refreshed == 1

    toggled = client.post("/api/mcp/installations/install-1/toggle", json={"enabled": True})
    assert toggled.status_code == 200
    assert toggled.json()["diagnostic"]["code"] == "mcp_server_toggled"

    tool_toggled = client.post(
        "/api/mcp/installations/install-1/tools/read_demo/toggle",
        json={"enabled": False, "confirm_side_effect": False},
    )
    assert tool_toggled.status_code == 200
    assert tool_toggled.json()["diagnostic"]["code"] == "mcp_tool_toggled"

    reloaded = client.post("/api/mcp/reload")
    assert reloaded.status_code == 200
    report = reloaded.json()["diagnostic"]
    assert report["code"] == "mcp_reload_partial"
    assert report["partial"] is True
    assert report["ok"] is False


def test_mcp_start_failure_marks_partial_without_raw_exception(monkeypatch) -> None:
    manager = _Manager()
    audit_actions = []

    async def fail_after_persist(_installation_id: str, enabled: bool):
        manager.current["desired_enabled"] = enabled
        manager.current["process_state"] = "error"
        raise RuntimeError("raw-process-secret")

    manager.toggle_installation = fail_after_persist
    client = _client(monkeypatch, manager, audit_actions)
    response = client.post("/api/mcp/installations/install-1/toggle", json={"enabled": True})
    assert response.status_code == 500
    report = response.json()["detail"]
    assert report["code"] == "mcp_server_start_partial"
    assert report["partial"] is True
    assert report["operation_id"] == "install-1"
    assert manager.current["desired_enabled"] is True
    assert "raw-process-secret" not in str(report)
    assert audit_actions[-1]["action"] == "mcp_toggle"
    assert audit_actions[-1]["outcome"] == "partial"


def test_mcp_tool_partial_authorization_is_audited(monkeypatch) -> None:
    manager = _Manager()
    audit_actions = []

    async def fail_after_persist(_installation_id: str, _remote_name: str, enabled: bool, **_kwargs):
        manager.current["tools"][0]["authorized"] = enabled
        raise RuntimeError("raw-tool-secret")

    manager.toggle_tool = fail_after_persist
    client = _client(monkeypatch, manager, audit_actions)
    response = client.post(
        "/api/mcp/installations/install-1/tools/read_demo/toggle",
        json={"enabled": False, "confirm_side_effect": False},
    )
    assert response.status_code == 500
    report = response.json()["detail"]
    assert report["code"] == "mcp_tool_toggle_partial"
    assert "raw-tool-secret" not in str(report)
    assert audit_actions[-1]["action"] == "mcp_tool_toggle"
    assert audit_actions[-1]["outcome"] == "partial"
    assert audit_actions[-1]["detail"]["enabled"] is False


def test_builtin_mcp_status_config_auth_and_preview_are_private(tmp_path, monkeypatch) -> None:
    paths = load_personification_module("plugin.personification.core.paths")
    data_store = load_personification_module("plugin.personification.core.data_store")
    management = load_personification_module("plugin.personification.core.mcp_management")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    monkeypatch.setattr(management, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    management.McpStore().ensure_builtin_social()

    manager = _BuiltinManager()
    client = _client(monkeypatch, manager)

    status = client.get("/api/mcp/builtin/social-research/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store, private"
    assert set(status.json()["platforms"]) == {"bilibili", "douyin", "tieba", "xiaoheihe"}
    assert status.json()["platforms"]["bilibili"]["config"] == {"quality_mode": "balanced"}

    configured = client.post(
        "/api/mcp/builtin/social-research/platforms/bilibili/configure",
        json={"enabled": True, "revision": 0, "config": {"quality_mode": "balanced"}},
    )
    assert configured.status_code == 200
    assert configured.json()["platform"]["revision"] == 1
    stale_revision = client.post(
        "/api/mcp/builtin/social-research/platforms/bilibili/configure",
        json={"enabled": False, "revision": 0, "config": {}},
    )
    assert stale_revision.status_code == 409
    assert stale_revision.json()["detail"]["code"] == "revision_conflict"

    invalid_config = client.post(
        "/api/mcp/builtin/social-research/platforms/bilibili/configure",
        json={"enabled": True, "revision": 1, "config": {"comment_limit": 201}},
    )
    assert invalid_config.status_code == 400
    assert invalid_config.json()["detail"]["code"] == "builtin_social_operation_failed"

    login = client.post("/api/mcp/builtin/social-research/auth/start", json={"platform": "bilibili"})
    assert login.status_code == 200
    assert manager.auth_requests[-1]["mode"] == "embedded_qr"
    manual_login = client.post(
        "/api/mcp/builtin/social-research/auth/start",
        json={"platform": "douyin", "mode": "manual_browser"},
    )
    assert manual_login.status_code == 200
    assert manager.auth_requests[-1]["mode"] == "manual_browser"
    assert "profile" not in manual_login.text.lower()
    qr = client.get(
        "/api/mcp/builtin/social-research/auth/session-1/qrcode",
        params={"platform": "bilibili"},
    )
    assert qr.status_code == 200
    assert qr.content == b"png-data"
    assert qr.headers["cache-control"] == "no-store, private"
    assert "owner" not in login.text

    wrong_logout = client.post(
        "/api/mcp/builtin/social-research/auth/logout",
        json={"platform": "bilibili", "confirm": "logout"},
    )
    assert wrong_logout.status_code == 400
    logout = client.post(
        "/api/mcp/builtin/social-research/auth/logout",
        json={"platform": "bilibili", "confirm": "确认注销B站"},
    )
    assert logout.status_code == 200

    preview = client.post(
        "/api/mcp/builtin/social-research/preview",
        json={"term": "刘涛", "context": "三角洲行动装备", "game": "三角洲行动"},
    )
    assert preview.status_code == 200
    assert preview.headers["cache-control"] == "no-store, private"
    assert preview.json()["packet"]["trust"] == "untrusted_data_only"
    assert manager.tool_calls[-1] == (
        "research_game_slang",
        {
            "term": "刘涛",
            "context": "三角洲行动装备",
            "game": "三角洲行动",
            "depth": "auto",
            "limit": 10,
        },
    )
    assert preview.json()["delivery"]["outbound_delivery"] == "not_applicable"

    search_preview = client.post(
        "/api/mcp/builtin/social-research/preview",
        json={
            "tool": "social_content_search",
            "term": "花来",
            "game": "三角洲行动",
            "limit": 7,
        },
    )
    assert search_preview.status_code == 200
    assert manager.tool_calls[-1] == (
        "social_content_search",
        {"query": "三角洲行动 花来", "limit": 7},
    )
    assert search_preview.json()["tool_name"] == "social_content_search"
