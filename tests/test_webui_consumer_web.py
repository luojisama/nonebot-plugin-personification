from __future__ import annotations

import base64

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def _service(runtime_context):  # noqa: ANN001, ANN202
    module = load_personification_module("plugin.personification.core.gemini_web_service")
    runtime = runtime_context.app_module.get_runtime_context()
    return module.get_gemini_web_service(runtime)


def _mimo_service(runtime_context):  # noqa: ANN001, ANN202
    module = load_personification_module("plugin.personification.core.mimo_web_asr_service")
    runtime = runtime_context.app_module.get_runtime_context()
    return module.get_mimo_web_asr_service(runtime)


def test_gemini_web_status_requires_admin_without_starting_helper(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    calls: list[bool] = []
    service = _service(_runtime_context)

    async def _status(_config, *, refresh=False):  # noqa: ANN001, ANN202
        calls.append(bool(refresh))
        return {
            "schema_version": 1,
            "enabled": False,
            "risk_acknowledged": False,
            "state": "disabled",
            "profile_present": False,
            "browser_running": False,
            "active_job": False,
            "interactive_session": None,
            "last_diagnostic_code": "gemini_web_disabled",
            "last_probe_at": 0,
        }

    monkeypatch.setattr(service, "status", _status)
    client = _build_client(_runtime_context)
    assert client.get("/personification/api/media/web/gemini/status").status_code == 401
    assert calls == []

    _login_as_admin(client, _runtime_context)
    response = client.get("/personification/api/media/web/gemini/status")
    assert response.status_code == 200
    assert response.json()["last_diagnostic_code"] == "gemini_web_disabled"
    assert calls == [False]
    assert "no-store" in response.headers.get("cache-control", "")


def test_gemini_web_probe_and_auth_use_bound_admin_owner(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    owners: list[str] = []
    service = _service(_runtime_context)

    async def _probe(_config):  # noqa: ANN001, ANN202
        return {"state": "login_required", "profile_present": False}

    async def _start(_config, owner):  # noqa: ANN001, ANN202
        owners.append(owner)
        return {
            "session_id": "auth_123",
            "status": "manual_verification_required",
            "interactive_available": True,
        }

    monkeypatch.setattr(service, "probe", _probe)
    monkeypatch.setattr(service, "auth_start", _start)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    assert client.post("/personification/api/media/web/gemini/probe").status_code == 200
    response = client.post("/personification/api/media/web/gemini/auth/start")
    assert response.status_code == 200
    assert response.json()["session_id"] == "auth_123"
    assert owners and owners[0].startswith("10001:") and owners[0].endswith(":gemini_web")


def test_gemini_web_frame_is_private_and_revisioned(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    service = _service(_runtime_context)
    observed: dict[str, object] = {}

    async def _frame(_config, session_id, owner, *, after_revision=0):  # noqa: ANN001, ANN202
        observed.update(session_id=session_id, owner=owner, revision=after_revision)
        return {
            "changed": True,
            "interactive_frame_revision": 7,
            "mime_type": "image/jpeg",
            "data_base64": base64.b64encode(b"jpeg-frame").decode("ascii"),
        }

    monkeypatch.setattr(service, "auth_frame", _frame)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.get("/personification/api/media/web/gemini/auth/auth_123/frame?revision=6")
    assert response.status_code == 200
    assert response.content == b"jpeg-frame"
    assert response.headers["X-Interactive-Revision"] == "7"
    assert response.headers["Content-Security-Policy"] == "default-src 'none'"
    assert observed["revision"] == 6
    assert str(observed["owner"]).endswith(":gemini_web")


def test_gemini_web_input_rejects_oversized_action_before_helper(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict] = []
    service = _service(_runtime_context)

    async def _input(_config, _session_id, _owner, action):  # noqa: ANN001, ANN202
        calls.append(action)
        return {"status": "manual_verification_required"}

    monkeypatch.setattr(service, "auth_input", _input)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post(
        "/personification/api/media/web/gemini/auth/auth_123/input",
        json={"action": {"type": "type", "text": "x" * (17 * 1024)}},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "gemini_web_request_invalid"
    assert calls == []


def test_gemini_web_network_risk_error_is_stable_and_sanitized(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    service = _service(_runtime_context)

    async def _probe(_config):  # noqa: ANN001, ANN202
        raise RuntimeError("gemini_web_network_risk_detected")

    monkeypatch.setattr(service, "probe", _probe)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post("/personification/api/media/web/gemini/probe")
    assert response.status_code == 503
    payload = response.json()["detail"]
    assert payload["code"] == "gemini_web_network_risk_detected"
    assert "Cookie" not in response.text
    assert "网络或账号安全风险" in payload["message"]


def test_mimo_web_asr_status_and_auth_use_generic_consumer_route(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    service = _mimo_service(_runtime_context)
    owners: list[str] = []

    async def _status(_config, *, refresh=False):  # noqa: ANN001, ANN202
        return {
            "state": "login_required",
            "profile_present": False,
            "page_contract_version": "mimo_studio_asr_v1",
            "refresh": refresh,
        }

    async def _start(_config, owner):  # noqa: ANN001, ANN202
        owners.append(owner)
        return {"session_id": "mimo_auth", "status": "manual_verification_required", "interactive_available": True}

    monkeypatch.setattr(service, "status", _status)
    monkeypatch.setattr(service, "auth_start", _start)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    status = client.get("/personification/api/media/web/mimo_asr/status")
    auth = client.post("/personification/api/media/web/mimo_asr/auth/start")
    assert status.status_code == 200
    assert status.json()["page_contract_version"] == "mimo_studio_asr_v1"
    assert auth.status_code == 200
    assert owners and owners[0].endswith(":mimo_asr_web")


def test_unknown_consumer_web_service_is_rejected(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    response = client.get("/personification/api/media/web/not-a-service/status")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "consumer_web_service_not_found"
