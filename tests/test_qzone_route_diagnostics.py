from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


data_store = load_personification_module("plugin.personification.core.data_store")
periodic_jobs = load_personification_module("plugin.personification.jobs.periodic_jobs")
qzone_auth = load_personification_module("plugin.personification.core.qzone_auth")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")
qzone_publish = load_personification_module("plugin.personification.core.qzone_publish")


class _Bot:
    self_id = "10000"

    def __init__(self, runtime_context) -> None:  # noqa: ANN001
        self._runtime_context = runtime_context

    async def call_api(self, _name, **kwargs):  # noqa: ANN001, ANN003
        self._runtime_context.sent.append(kwargs)
        return {"message_id": 1}

    async def send_private_msg(self, **kwargs):  # noqa: ANN003
        self._runtime_context.sent.append(kwargs)
        return {"message_id": 1}


def _logger() -> SimpleNamespace:
    return SimpleNamespace(
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
    )


def _install_runtime(_runtime_context, *, bundle, connected: bool = True) -> None:  # noqa: ANN001
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=(lambda: {"10000": _Bot(_runtime_context)}) if connected else (lambda: {}),
        logger=_logger(),
        runtime_bundle=bundle,
    )


def _admin_client(_runtime_context):  # noqa: ANN001, ANN202
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf
    return client


def _assert_safe_report(report: dict, *, code: str, phase: str, outcome_unknown: bool = False) -> None:
    assert report["code"] == code
    assert report["phase"] == phase
    assert isinstance(report["steps"], list)
    assert report["outcome_unknown"] is outcome_unknown


def test_qzone_status_sanitizes_all_last_errors_and_adds_diagnostic(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    secret = "raw-status-secret p_skey=status-cookie-secret"
    data_store.get_data_store().save_sync(
        "qzone_social_state",
        {
            "last_error": secret,
            "last_result": {"ok": False, "last_error": secret, "cookie": "uin=o1; p_skey=nested-secret"},
            "last_inbound_error": secret,
            "last_inbound_result": {"ok": False, "last_error": secret},
        },
    )
    monkeypatch.setattr(
        qzone_service,
        "get_qzone_auth_status",
        lambda _bot_id="": {"status": "refresh_failed", "last_error": secret, "token": "token-secret"},
    )
    _install_runtime(_runtime_context, bundle=SimpleNamespace(qzone_publish_available=True))
    client = _admin_client(_runtime_context)

    response = client.get("/personification/api/qzone/status")

    assert response.status_code == 200
    body = response.json()
    _assert_safe_report(body["diagnostic"], code="qzone_status_loaded", phase="status_snapshot")
    assert body["runtime"]["build_id"]
    assert body["runtime"]["worker_id"]
    assert body["runtime"]["process_started_at"] > 0
    assert {item["label"] for item in body["diagnostic"]["details"]} >= {
        "Build",
        "Worker",
        "Process started",
    }
    assert body["auth"]["last_error"] == body["social"]["last_error"] == body["inbound"]["last_error"]
    assert body["auth_by_bot"]["10000"]["last_error"] == body["auth"]["last_error"]
    assert body["social"]["last_result"]["last_error"] == body["social"]["last_error"]
    assert body["auth"]["token"] == "***"
    assert "raw-status-secret" not in response.text
    assert "status-cookie-secret" not in response.text
    assert "nested-secret" not in response.text


def test_qzone_status_exposes_per_bot_read_only_capabilities_without_credentials(
    _runtime_context,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        _runtime_context.plugin_config,
        "personification_qzone_enabled",
        True,
        raising=False,
    )
    with qzone_service._AUTH_STATE_LOCK:
        qzone_service._AUTH_STATES.clear()
    qzone_service._set_qzone_capability("10000", "qzone.cookie_export", "available", "onebot_cookie_verified")
    qzone_service._set_qzone_capability("10000", "qzone.web_read", "available", "feed_probe_succeeded")
    qzone_service._set_qzone_capability("10000", "qzone.web_write", "unknown", "not_destructively_probed")
    _install_runtime(_runtime_context, bundle=SimpleNamespace(qzone_publish_available=True))
    client = _admin_client(_runtime_context)

    response = client.get("/personification/api/qzone/status")

    assert response.status_code == 200
    body = response.json()
    capabilities = body["capabilities_by_bot"]["10000"]
    assert set(capabilities) == {
        "qzone.cookie_export",
        "qzone.web_read",
        "qzone.web_write",
        "read_only",
        "write_available",
    }
    assert capabilities["qzone.cookie_export"]["state"] == "available"
    assert capabilities["qzone.web_read"]["state"] == "available"
    assert capabilities["qzone.web_write"]["state"] == "unknown"
    assert capabilities["read_only"] is True
    assert body["read_only"] is True
    assert body["publish_available"] is False
    for name in ("qzone.cookie_export", "qzone.web_read", "qzone.web_write"):
        assert set(capabilities[name]) == {"state", "reason_code", "updated_at"}
        assert "p_skey" not in str(capabilities[name]).lower()
        assert "credential" not in str(capabilities[name]).lower()


def test_qzone_status_projects_unresolved_operations_without_cache(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        qzone_publish,
        "list_qzone_publish_operations",
        lambda **_kwargs: [{
            "operation_id": "unknown-op",
            "bot_id": "10000",
            "kind": "post",
            "status": "unknown",
            "created_at": 1,
            "dispatch_started_at": 2,
            "result_code": "dispatch_timeout",
            "payload": {"content": "安全正文", "cookie": "must-not-leak"},
        }],
    )
    _install_runtime(_runtime_context, bundle=SimpleNamespace(qzone_publish_available=True))
    client = _admin_client(_runtime_context)

    response = client.get("/personification/api/qzone/status")
    body = response.json()

    assert response.headers["cache-control"] == "no-store, private"
    assert body["reconciliation"]["state"] == "unknown"
    assert body["reconciliation"]["operations"][0]["operation_id"] == "unknown-op"
    assert body["reconciliation"]["operations"][0]["content"] == "安全正文"
    assert "must-not-leak" not in response.text


def test_qzone_operation_reconcile_and_history_routes(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    feed = {
        "feed_id": "remote-0448",
        "owner_uin": "10000",
        "content": "远端漏记正文",
        "created_at": 1783918080,
    }

    class _Service:
        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            return True, "ok", [feed]

    monkeypatch.setattr(
        qzone_publish,
        "get_qzone_publish_operation",
        lambda _operation_id: {
            "operation_id": "unknown-op",
            "bot_id": "10000",
            "kind": "post",
            "status": "unknown",
            "payload": {"content": "远端漏记正文"},
        },
    )

    async def _reconcile(**_kwargs):  # noqa: ANN003
        return {"ok": True, "status": "succeeded", "changed": True, "operation_id": "unknown-op"}

    monkeypatch.setattr(qzone_publish, "reconcile_qzone_publish_from_self_feed", _reconcile)
    monkeypatch.setattr(
        qzone_publish,
        "resolve_qzone_publish_absent",
        lambda **_kwargs: {"ok": True, "status": "definite_failure", "changed": True, "operation_id": "unknown-op"},
    )
    monkeypatch.setattr(
        qzone_publish,
        "record_historical_qzone_feed",
        lambda **_kwargs: {
            "ok": True,
            "status": "succeeded",
            "operation_id": "legacy-reconciled",
            "remote_time": feed["created_at"],
            "newly_committed": True,
        },
    )
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(qzone_social_service=_Service(), qzone_publish_available=True),
    )
    client = _admin_client(_runtime_context)

    detail_response = client.get("/personification/api/qzone/operations/unknown-op")
    reconcile_response = client.post(
        "/personification/api/qzone/operations/unknown-op/reconcile",
        json={"bot_id": "10000"},
    )
    absent_response = client.post(
        "/personification/api/qzone/operations/unknown-op/resolve-absent",
        json={"bot_id": "10000"},
    )
    candidates_response = client.get(
        "/personification/api/qzone/reconcile-candidates?bot_id=10000"
    )
    history_response = client.post(
        "/personification/api/qzone/reconcile-history",
        json={"bot_id": "10000", "feed_id": "remote-0448"},
    )

    assert detail_response.headers["cache-control"] == "no-store, private"
    assert detail_response.json()["operation"]["status"] == "unknown"
    assert reconcile_response.json()["code"] == "qzone_reconcile_succeeded"
    assert absent_response.json()["code"] == "qzone_operation_confirmed_absent"
    assert candidates_response.headers["cache-control"] == "no-store, private"
    assert candidates_response.json()["candidates"][0]["feed_id"] == "remote-0448"
    assert history_response.json()["code"] == "qzone_history_reconciled"


def test_qzone_post_preflight_errors_are_structured_and_safe(_runtime_context) -> None:
    operation_id = "preflight-op"
    _install_runtime(_runtime_context, bundle=SimpleNamespace())
    client = _admin_client(_runtime_context)

    unavailable = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": operation_id},
    )

    assert unavailable.status_code == 503
    report = unavailable.json()["detail"]
    _assert_safe_report(report, code="qzone_post_capability_unavailable", phase="post_preflight")
    assert report["operation_id"] == operation_id
    assert [item["key"] for item in report["steps"]] == ["capabilities"]

    class _BrokenBundle:
        @property
        def qzone_generate_post(self):  # noqa: ANN201
            raise RuntimeError("raw-preflight-runtime-secret")

    _install_runtime(_runtime_context, bundle=_BrokenBundle())
    client = _admin_client(_runtime_context)
    broken = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": operation_id},
    )
    assert broken.status_code == 500
    report = broken.json()["detail"]
    _assert_safe_report(report, code="qzone_post_preflight_exception", phase="post_preflight")
    assert report["trace_id"]
    assert "raw-preflight-runtime-secret" not in broken.text


def test_qzone_post_requires_operation_id(_runtime_context) -> None:
    _install_runtime(_runtime_context, bundle=SimpleNamespace())
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000"},
    )

    assert response.status_code == 400
    report = response.json()["detail"]
    _assert_safe_report(report, code="qzone_operation_id_missing", phase="input_validation")


def test_qzone_post_duplicate_success_does_not_mark_generated_content(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    marked: list[str] = []

    async def generate(_bot):  # noqa: ANN001
        return "重复请求正文"

    setattr(generate, "mark_published", lambda content: marked.append(content))

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("duplicate operation must not dispatch")

    async def coordinated(**_kwargs):  # noqa: ANN003
        return {
            "success": True,
            "status": "succeeded",
            "duplicate": True,
            "newly_committed": False,
            "state": {},
        }

    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", coordinated)
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(qzone_generate_post=generate, publish_qzone_shuo=publish),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "duplicate-success"},
    )

    assert response.status_code == 200
    assert response.json()["duplicate"] is True
    assert marked == []


def test_qzone_post_hides_refresh_and_publish_service_messages(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(qzone_service, "get_qzone_auth_status", lambda _bot_id="": {"status": "healthy"})

    async def generate(_bot):  # noqa: ANN001
        return "一条安全草稿"

    async def publish(_content, _bot_id):  # noqa: ANN001
        return False, "publish-service-secret"

    async def refresh(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        return False, "cookie=refresh-service-secret"

    async def coordinated(**_kwargs):  # noqa: ANN003
        return {"success": False, "status": "failed", "message": "raw-publish-service-secret"}

    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", coordinated)
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(
            qzone_generate_post=generate,
            publish_qzone_shuo=publish,
            update_qzone_cookie=refresh,
        ),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "publish-failed-op"},
    )

    assert response.status_code == 200
    body = response.json()
    _assert_safe_report(body, code="qzone_publish_rejected", phase="qzone_publish")
    assert body["retryable"] is True
    assert "raw-publish-service-secret" not in response.text
    assert "refresh-service-secret" not in response.text

    async def unknown(**_kwargs):  # noqa: ANN003
        return {
            "success": False,
            "status": "outcome_unknown",
            "message": "raw-timeout-secret",
            "result_code": "auth_page_2xx",
            "publish_detail": {"image_requested": False, "cookie": "must-not-leak"},
        }

    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", unknown)
    unknown_response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "publish-unknown-op"},
    )
    assert unknown_response.status_code == 200
    unknown_body = unknown_response.json()
    assert unknown_body["code"] == "qzone_publish_outcome_unknown"
    assert unknown_body["outcome_unknown"] is True
    assert unknown_body["retryable"] is False
    assert any(
        item["label"] == "发布结果代码" and item["value"] == "auth_page_2xx"
        for item in unknown_body["details"]
    )
    assert unknown_body["steps"][-1]["details"][0]["value"] == "auth_page_2xx"
    assert "raw-timeout-secret" not in unknown_response.text
    assert "must-not-leak" not in unknown_response.text


def test_qzone_post_reports_image_upload_failure_before_publish(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    async def generate(_bot):  # noqa: ANN001
        return "配图发布正文 [IMAGE_B64]QUJDREVGRw==[/IMAGE_B64]"

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("coordinator stub owns the result")

    async def coordinated(**_kwargs):  # noqa: ANN003
        return {
            "success": False,
            "status": "definite_failure",
            "result_code": "image_upload_missing_pic_bo",
            "publish_detail": {
                "image_requested": True,
                "image_uploaded": False,
                "image_mime_type": "image/png",
            },
        }

    monkeypatch.setattr(qzone_service, "get_qzone_auth_status", lambda _bot_id="": {"status": "healthy"})
    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", coordinated)
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(qzone_generate_post=generate, publish_qzone_shuo=publish),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "image-upload-failed-op"},
    )

    body = response.json()
    _assert_safe_report(body, code="qzone_image_upload_failed", phase="qzone_image_upload")
    assert body["retryable"] is True
    assert body["outcome_unknown"] is False
    assert body["steps"][-1]["key"] == "image_upload"
    assert body["content"] == "配图发布正文 [配图]"
    assert "IMAGE_B64" not in response.text
    assert "QUJDREVGRw" not in response.text
    assert {item["label"] for item in body["steps"][-1]["details"]} >= {
        "结果代码",
        "配图上传",
        "配图格式",
    }


def test_qzone_post_stops_before_generation_when_forced_refresh_still_requires_login(
    _runtime_context,
    monkeypatch,
) -> None:  # noqa: ANN001
    refresh_calls: list[bool] = []

    async def refresh(_bot, *, force=False):  # noqa: ANN001
        refresh_calls.append(force)
        return False, "p_skey=must-not-leak"

    async def generate(_bot):  # noqa: ANN001
        raise AssertionError("login preflight must stop before generation")

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("login preflight must stop before publish")

    monkeypatch.setattr(qzone_service, "get_qzone_auth_status", lambda _bot_id="": {"status": "login_required"})
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(
            qzone_generate_post=generate,
            publish_qzone_shuo=publish,
            update_qzone_cookie=refresh,
        ),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "login-preflight-op"},
    )

    assert response.status_code == 200
    body = response.json()
    _assert_safe_report(body, code="qzone_login_required", phase="qzone_auth")
    assert body["retryable"] is False
    assert body["operation_id"] == "login-preflight-op"
    assert [item["key"] for item in body["steps"]] == ["cookie_refresh"]
    assert refresh_calls == [True]
    assert "must-not-leak" not in response.text


def test_qzone_post_stops_before_generation_when_forced_refresh_hits_risk_challenge(
    _runtime_context,
    monkeypatch,
) -> None:  # noqa: ANN001
    async def refresh(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        return False, "risk-page-secret"

    async def generate(_bot):  # noqa: ANN001
        raise AssertionError("risk preflight must stop before generation")

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("risk preflight must stop before publish")

    monkeypatch.setattr(qzone_service, "get_qzone_auth_status", lambda _bot_id="": {"status": "risk_blocked"})
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(
            qzone_generate_post=generate,
            publish_qzone_shuo=publish,
            update_qzone_cookie=refresh,
        ),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "risk-preflight-op"},
    )

    body = response.json()
    assert response.status_code == 200
    _assert_safe_report(body, code="qzone_risk_blocked", phase="qzone_auth")
    assert body["retryable"] is False
    assert body["operation_id"] == "risk-preflight-op"
    assert "risk-page-secret" not in response.text


@pytest.mark.parametrize(
    ("publish_status", "expected_code", "expected_phase", "expected_retryable", "expected_unknown"),
    [
        ("definite_failure", "qzone_login_required", "qzone_auth", True, False),
        ("outcome_unknown", "qzone_publish_outcome_unknown", "qzone_publish", False, True),
    ],
)
def test_qzone_post_recovers_login_without_automatically_replaying_write(
    _runtime_context,
    monkeypatch,
    publish_status: str,
    expected_code: str,
    expected_phase: str,
    expected_retryable: bool,
    expected_unknown: bool,
) -> None:  # noqa: ANN001
    auth_state = {"status": "healthy"}
    refresh_calls: list[bool] = []
    coordinated_calls: list[str] = []

    async def refresh(_bot, *, force=False):  # noqa: ANN001
        refresh_calls.append(force)
        if len(refresh_calls) > 1:
            auth_state["status"] = "healthy"
        return True, "p_skey=must-not-leak"

    async def generate(_bot):  # noqa: ANN001
        return "登录恢复测试草稿"

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("coordinator stub owns the single dispatch attempt")

    async def coordinated(**_kwargs):  # noqa: ANN003
        coordinated_calls.append(publish_status)
        auth_state["status"] = "login_required"
        return {"success": False, "status": publish_status, "message": "secret"}

    monkeypatch.setattr(qzone_service, "get_qzone_auth_status", lambda _bot_id="": dict(auth_state))
    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", coordinated)
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(
            qzone_generate_post=generate,
            publish_qzone_shuo=publish,
            update_qzone_cookie=refresh,
        ),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": f"login-recovery-{publish_status}"},
    )

    assert response.status_code == 200
    body = response.json()
    _assert_safe_report(body, code=expected_code, phase=expected_phase, outcome_unknown=expected_unknown)
    assert body["retryable"] is expected_retryable
    assert [item["key"] for item in body["steps"]][-2:] == ["publish", "auth_recovery"]
    assert refresh_calls == [True, True]
    assert coordinated_calls == [publish_status]
    assert "must-not-leak" not in response.text


def test_qzone_post_orchestration_exception_is_safe_and_marked_unknown(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    async def generate(_bot):  # noqa: ANN001
        return "一条安全草稿"

    async def publish(_content, _bot_id):  # noqa: ANN001
        return True, "ok"

    async def coordinated(**_kwargs):  # noqa: ANN003
        raise RuntimeError("raw-orchestration-secret")

    monkeypatch.setattr(periodic_jobs, "coordinated_qzone_publish", coordinated)
    _install_runtime(
        _runtime_context,
        bundle=SimpleNamespace(qzone_generate_post=generate, publish_qzone_shuo=publish),
    )
    client = _admin_client(_runtime_context)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "orchestration-op"},
    )

    assert response.status_code == 500
    report = response.json()["detail"]
    _assert_safe_report(report, code="qzone_publish_orchestration_exception", phase="qzone_publish", outcome_unknown=True)
    assert report["retryable"] is False
    assert report["partial"] is True
    assert report["steps"][-1]["status"] == "unknown"
    assert "raw-orchestration-secret" not in response.text


def test_qzone_refresh_cookie_diagnostics_hide_return_and_exception_messages(_runtime_context) -> None:
    async def failed(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        return False, "p_skey=refresh-return-secret"

    bundle = SimpleNamespace(update_qzone_cookie=failed)
    _install_runtime(_runtime_context, bundle=bundle)
    client = _admin_client(_runtime_context)

    failed_response = client.post("/personification/api/qzone/refresh-cookie", json={"bot_id": "10000"})
    assert failed_response.status_code == 200
    body = failed_response.json()
    assert body["status"] == "failed"
    _assert_safe_report(body["diagnostic"], code="qzone_cookie_refresh_failed", phase="cookie_refresh")
    assert "refresh-return-secret" not in failed_response.text

    async def crashed(_bot, *, force=False):  # noqa: ANN001
        raise RuntimeError("raw-refresh-runtime-secret")

    bundle.update_qzone_cookie = crashed
    crashed_response = client.post("/personification/api/qzone/refresh-cookie", json={"bot_id": "10000"})
    assert crashed_response.status_code == 200
    body = crashed_response.json()
    _assert_safe_report(body["diagnostic"], code="qzone_cookie_refresh_exception", phase="cookie_refresh")
    assert body["diagnostic"]["trace_id"]
    assert "raw-refresh-runtime-secret" not in crashed_response.text


def test_qzone_login_exceptions_are_structured_and_preserve_qr_response(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    _install_runtime(_runtime_context, bundle=SimpleNamespace())
    client = _admin_client(_runtime_context)

    async def blocked(**_kwargs):  # noqa: ANN003
        raise RuntimeError("raw-login-busy-secret")

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "start", blocked)
    started = client.post("/personification/api/qzone/auth/login/start", json={"bot_id": "10000"})
    assert started.status_code == 429
    _assert_safe_report(started.json()["detail"], code="qzone_login_start_blocked", phase="login_start")
    assert "raw-login-busy-secret" not in started.text

    def status_crashed(_session_id, *, owner_key):  # noqa: ANN001
        raise RuntimeError("raw-login-status-secret")

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "status", status_crashed)
    status = client.get("/personification/api/qzone/auth/login/session-one/status")
    assert status.status_code == 500
    _assert_safe_report(status.json()["detail"], code="qzone_login_status_exception", phase="login_status")
    assert "raw-login-status-secret" not in status.text

    monkeypatch.setattr(
        qzone_auth.qzone_login_manager,
        "qrcode",
        lambda _session_id, *, owner_key: b"private-qr-png",
    )
    image = client.get("/personification/api/qzone/auth/login/session-one/qrcode")
    assert image.status_code == 200
    assert image.content == b"private-qr-png"
    assert image.headers["content-type"].startswith("image/png")
    assert "no-store" in image.headers["cache-control"]

    def qrcode_missing(_session_id, *, owner_key):  # noqa: ANN001
        raise LookupError("raw-qrcode-service-secret")

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "qrcode", qrcode_missing)
    missing_image = client.get("/personification/api/qzone/auth/login/session-one/qrcode")
    assert missing_image.status_code == 404
    _assert_safe_report(
        missing_image.json()["detail"],
        code="qzone_login_qrcode_not_found",
        phase="login_qrcode",
    )
    assert "raw-qrcode-service-secret" not in missing_image.text

    async def cancel_crashed(_session_id, *, owner_key):  # noqa: ANN001
        raise RuntimeError("raw-login-cancel-secret")

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "cancel", cancel_crashed)
    cancelled = client.post("/personification/api/qzone/auth/login/session-one/cancel")
    assert cancelled.status_code == 500
    _assert_safe_report(cancelled.json()["detail"], code="qzone_login_cancel_exception", phase="login_cancel")
    assert "raw-login-cancel-secret" not in cancelled.text


def test_qzone_login_success_preserves_session_fields_with_diagnostic(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    async def start(**_kwargs):  # noqa: ANN003
        return {
            "session_id": "session-preserved",
            "bot_id": "10000",
            "status": "waiting_scan",
            "message": "raw-upstream-message",
            "created_at": 1.0,
            "updated_at": 2.0,
            "expires_at": 3.0,
            "expires_in_seconds": 120,
            "terminal": False,
            "qr_ready": True,
        }

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "start", start)
    _install_runtime(_runtime_context, bundle=SimpleNamespace())
    client = _admin_client(_runtime_context)

    response = client.post("/personification/api/qzone/auth/login/start", json={"bot_id": "10000"})

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == "session-preserved"
    assert body["bot_id"] == "10000"
    assert body["status"] == "waiting_scan"
    assert body["terminal"] is False
    assert body["qr_ready"] is True
    assert body["expires_in_seconds"] == 120
    _assert_safe_report(body["diagnostic"], code="qzone_login_started", phase="login_start")
    assert "raw-upstream-message" not in response.text


def test_qzone_cookie_import_diagnostics_never_echo_unknown_reason_or_exception(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    secret = "uin=o10000; p_skey=manual-input-secret;"

    async def rejected(**_kwargs):  # noqa: ANN003
        return False, "raw-cookie-service-secret"

    monkeypatch.setattr(qzone_service, "install_qzone_cookie", rejected)
    _install_runtime(_runtime_context, bundle=SimpleNamespace())
    client = _admin_client(_runtime_context)

    rejected_response = client.post(
        "/personification/api/qzone/auth/cookie",
        json={"bot_id": "10000", "cookie": secret},
    )
    assert rejected_response.status_code == 200
    body = rejected_response.json()
    assert body["status"] == "failed"
    _assert_safe_report(body["diagnostic"], code="qzone_cookie_validation_failed", phase="cookie_validation")
    assert "manual-input-secret" not in rejected_response.text
    assert "raw-cookie-service-secret" not in rejected_response.text

    async def crashed(**_kwargs):  # noqa: ANN003
        raise RuntimeError("raw-cookie-runtime-secret")

    monkeypatch.setattr(qzone_service, "install_qzone_cookie", crashed)
    crashed_response = client.post(
        "/personification/api/qzone/auth/cookie",
        json={"bot_id": "10000", "cookie": secret},
    )
    assert crashed_response.status_code == 500
    _assert_safe_report(crashed_response.json()["detail"], code="qzone_cookie_import_exception", phase="cookie_install")
    assert "manual-input-secret" not in crashed_response.text
    assert "raw-cookie-runtime-secret" not in crashed_response.text


def test_qzone_scan_diagnostics_sanitize_failures_and_exceptions(_runtime_context) -> None:
    async def failed(*, force=False):
        assert force is True
        return {
            "ok": False,
            "status": "failed",
            "feeds_seen": 2,
            "last_error": "raw-scan-service-secret p_skey=scan-cookie-secret",
            "cookie": "uin=o1; p_skey=nested-scan-secret",
        }

    bundle = SimpleNamespace(qzone_social_scan=failed)
    _install_runtime(_runtime_context, bundle=bundle)
    client = _admin_client(_runtime_context)

    failed_response = client.post("/personification/api/qzone/scan-now", json={"kind": "social"})
    assert failed_response.status_code == 200
    body = failed_response.json()
    assert body["feeds_seen"] == 2
    assert body["cookie"] == "***"
    _assert_safe_report(body["diagnostic"], code="qzone_social_scan_failed", phase="scan_execute")
    assert "raw-scan-service-secret" not in failed_response.text
    assert "scan-cookie-secret" not in failed_response.text
    assert "nested-scan-secret" not in failed_response.text

    async def crashed(*, force=False):
        raise RuntimeError("raw-scan-runtime-secret")

    bundle.qzone_social_scan = crashed
    crashed_response = client.post("/personification/api/qzone/scan-now", json={"kind": "social"})
    assert crashed_response.status_code == 500
    _assert_safe_report(crashed_response.json()["detail"], code="qzone_social_scan_exception", phase="scan_execute")
    assert "raw-scan-runtime-secret" not in crashed_response.text

    invalid = client.post("/personification/api/qzone/scan-now", json={"kind": "unsupported"})
    assert invalid.status_code == 400
    _assert_safe_report(invalid.json()["detail"], code="qzone_scan_kind_invalid", phase="scan_preflight")
