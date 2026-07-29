from __future__ import annotations
import json

from types import SimpleNamespace

import httpx

from ._loader import load_personification_module
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


def test_config_value_reports_persistence_and_runtime_reload(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    monkeypatch.setattr(config_routes, "_schedule_diagnostics_warm", lambda _runtime: None)
    reload_calls: list[str] = []
    runtime = _runtime_context.app_module.get_runtime_context()
    runtime.runtime_bundle = SimpleNamespace(
        reload_runtime_services=lambda: reload_calls.append("reload")
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post(
        "/personification/api/config/value",
        json={"field_name": "personification_agent_max_steps", "value": "7"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["errors"] == []
    assert body["env_json_path"]
    assert body["dotenv_path"] is None
    assert body["new_value"] == 7
    assert body["diagnostic"]["code"] == "config_value_updated"
    steps = {item["key"]: item for item in body["diagnostic"]["steps"]}
    assert steps["persist_config"]["status"] == "ok"
    assert steps["runtime_config_sync"]["status"] == "ok"
    assert steps["runtime_reload"]["status"] == "ok"
    assert any(item["label"] == ".env.prod / .env" for item in steps["persist_config"]["details"])
    assert reload_calls == ["reload"]


def test_config_value_unexpected_exception_is_structured_and_safe(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")

    def _raise(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("https://api.example.test/models?api_key=super-secret")

    monkeypatch.setattr(config_routes.env_writer, "write_both", _raise)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post(
        "/personification/api/config/value",
        json={"field_name": "personification_agent_max_steps", "value": 6},
    )

    assert response.status_code == 500
    diagnostic = response.json()["detail"]
    assert diagnostic["code"] == "config_value_persist_exception"
    assert diagnostic["trace_id"]
    assert diagnostic["details"] == [
        {"label": "异常类型", "value": "RuntimeError", "status": "error"}
    ]
    rendered = str(diagnostic)
    assert "api.example.test" not in rendered
    assert "super-secret" not in rendered


def test_apply_recommended_reports_unknown_field_skip_reason(_runtime_context) -> None:  # noqa: ANN001
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post(
        "/personification/api/config/apply-recommended",
        json={"fields": ["personification_not_a_recommended_field"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == []
    assert body["skipped"] == [
        {
            "field_name": "personification_not_a_recommended_field",
            "reason": "不在推荐默认值列表",
        }
    ]
    assert body["diagnostic"]["code"] == "config_recommended_failed"
    assert any(
        item["label"] == "跳过 personification_not_a_recommended_field"
        and item["value"] == "不在推荐默认值列表"
        for item in body["diagnostic"]["details"]
    )


def test_provider_model_probe_classifies_failures_without_raw_exception_data(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    request = httpx.Request(
        "GET",
        "https://user:password@api.example.test/models?api_key=super-secret",
    )
    auth_error = httpx.HTTPStatusError(
        "auth failed at raw URL",
        request=request,
        response=httpx.Response(401, request=request),
    )
    auth_error.auth_mode = "bearer"
    auth_error.request_count = 2
    cases = [
        (auth_error, "provider_model_probe_auth_failed"),
        (
            httpx.HTTPStatusError(
                "permission failed at raw URL",
                request=request,
                response=httpx.Response(403, request=request),
            ),
            "provider_model_probe_permission_denied",
        ),
        (httpx.ReadTimeout("raw timeout URL", request=request), "provider_model_probe_timeout"),
        (httpx.ConnectError("raw network URL", request=request), "provider_model_probe_network_failed"),
        (
            httpx.HTTPStatusError(
                "HTTP failed at raw URL",
                request=request,
                response=httpx.Response(503, request=request),
            ),
            "provider_model_probe_http_failed",
        ),
        (ValueError("parse failed at https://api.example.test/?token=super-secret"), "provider_model_probe_parse_failed"),
        (RuntimeError("unexpected https://api.example.test/?token=super-secret"), "provider_model_probe_exception"),
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    for raised, expected_code in cases:
        async def _fail(_provider, error=raised):  # noqa: ANN001, ANN202
            raise error

        monkeypatch.setattr(config_routes, "_probe_http_models", _fail)
        response = client.post(
            "/personification/api/config/provider-models",
            json={
                "provider": {
                    "api_type": "openai",
                    "api_url": "https://api.example.test/v1?api_key=super-secret",
                    "api_key": "super-secret",
                }
            },
        )

        assert response.status_code == 502
        diagnostic = response.json()["detail"]
        assert diagnostic["code"] == expected_code
        assert diagnostic["steps"]
        rendered = str(diagnostic)
        assert "api.example.test" not in rendered
        assert "super-secret" not in rendered
        if raised is auth_error:
            details = {item["label"]: item["value"] for item in diagnostic["details"]}
            assert details["Auth mode"] == "bearer"
            assert details["Request count"] == 2


def test_provider_model_probe_success_preserves_fields_and_adds_diagnostic(
    _runtime_context,  # noqa: ANN001
) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.post(
        "/personification/api/config/provider-models",
        json={"provider": {"api_type": "openai_codex", "model": "gpt-test-codex"}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_type"] == "openai_codex"
    assert body["source"] == "local_cache"
    assert body["manual_allowed"] is True
    assert body["models"]
    assert body["diagnostic"]["code"] == "provider_model_probe_complete"
    assert body["diagnostic"]["ok"] is True


def test_remote_provider_probe_does_not_return_full_endpoint(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")

    async def _success(_provider):  # noqa: ANN001, ANN202
        return ([{"id": "safe-model", "label": "safe-model"}], "https://private.example.test/v1/models?token=secret")

    monkeypatch.setattr(config_routes, "_probe_http_models", _success)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    response = client.post(
        "/personification/api/config/provider-models",
        json={"provider": {"api_type": "openai", "api_url": "https://private.example.test/v1", "api_key": "secret"}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["source"] == "remote_models"
    assert "private.example.test" not in response.text
    assert "secret" not in response.text


def test_config_entries_recursively_masks_provider_secrets(_runtime_context) -> None:  # noqa: ANN001
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "openai",
            "api_url": "https://api.example.test/v1",
            "api_key": "nested-provider-secret",
            "headers": {"Authorization": "Bearer nested-header-secret"},
            "model": "gpt-test",
        }
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.get("/personification/api/config/entries")

    assert response.status_code == 200
    entry = next(
        item
        for item in response.json()["entries"]
        if item["field_name"] == "personification_api_pools"
    )
    assert entry["current"][0]["api_key"] == "***"
    assert entry["current"][0]["headers"] == "***"
    assert entry["current"][0]["_secret_ref"]
    assert "nested-provider-secret" not in response.text
    assert "nested-header-secret" not in response.text


def test_config_api_pool_mask_placeholder_preserves_existing_secret(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    monkeypatch.setattr(config_routes, "_schedule_diagnostics_warm", lambda _runtime: None)
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "openai",
            "api_key": "existing-provider-secret",
            "model": "gpt-old",
        }
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    listed = client.get("/personification/api/config/entries").json()
    provider_view = next(
        item
        for item in listed["entries"]
        if item["field_name"] == "personification_api_pools"
    )["current"][0]

    response = client.post(
        "/personification/api/config/value",
        json={
            "field_name": "personification_api_pools",
            "value": [
                {
                    "name": "primary",
                    "api_type": "openai",
                    "api_key": "***",
                    "model": "gpt-new",
                    "_secret_ref": provider_view["_secret_ref"],
                }
            ],
        },
    )

    assert response.status_code == 200, response.text
    assert _runtime_context.plugin_config.personification_api_pools[0]["api_key"] == "existing-provider-secret"
    assert response.json()["new_value"][0]["api_key"] == "***"
    assert "existing-provider-secret" not in response.text
    serialized = json.dumps(response.json(), ensure_ascii=False)
    assert "gpt-new" in serialized


def test_config_api_pool_secret_refs_survive_duplicate_names_and_reorder(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    monkeypatch.setattr(config_routes, "_schedule_diagnostics_warm", lambda _runtime: None)
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "duplicate",
            "api_type": "openai",
            "api_url": "https://a.example/v1",
            "api_key": "secret-a",
            "model": "model-a",
        },
        {
            "name": "duplicate",
            "api_type": "openai",
            "api_url": "https://b.example/v1",
            "api_key": "secret-b",
            "model": "model-b",
        },
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    listed = client.get("/personification/api/config/entries").json()
    views = next(
        item
        for item in listed["entries"]
        if item["field_name"] == "personification_api_pools"
    )["current"]

    response = client.post(
        "/personification/api/config/value",
        json={
            "field_name": "personification_api_pools",
            "value": [views[1], views[0]],
        },
    )

    assert response.status_code == 200, response.text
    saved = _runtime_context.plugin_config.personification_api_pools
    assert [(item["api_url"], item["api_key"]) for item in saved] == [
        ("https://b.example/v1", "secret-b"),
        ("https://a.example/v1", "secret-a"),
    ]
    assert "secret-a" not in response.text
    assert "secret-b" not in response.text


def test_config_api_pool_requires_secret_refresh_after_transport_change(
    _runtime_context  # noqa: ANN001
) -> None:
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "openai",
            "api_url": "https://old.example/v1",
            "api_key": "transport-bound-secret",
            "model": "model-a",
        }
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    listed = client.get("/personification/api/config/entries").json()
    provider = next(
        item
        for item in listed["entries"]
        if item["field_name"] == "personification_api_pools"
    )["current"][0]
    provider["api_url"] = "https://new.example/v1"

    response = client.post(
        "/personification/api/config/value",
        json={"field_name": "personification_api_pools", "value": [provider]},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_value_invalid"
    assert _runtime_context.plugin_config.personification_api_pools[0]["api_key"] == "transport-bound-secret"


def test_config_api_pool_requires_secret_refresh_after_gemini_auth_change(
    _runtime_context  # noqa: ANN001
) -> None:
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "gemini",
            "api_url": "https://gemini.example",
            "api_key": "auth-bound-secret",
            "gemini_auth_mode": "auto",
            "model": "gemini-test",
        }
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    listed = client.get("/personification/api/config/entries").json()
    provider = next(
        item
        for item in listed["entries"]
        if item["field_name"] == "personification_api_pools"
    )["current"][0]
    provider["gemini_auth_mode"] = "bearer"

    response = client.post(
        "/personification/api/config/value",
        json={"field_name": "personification_api_pools", "value": [provider]},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_value_invalid"
    assert _runtime_context.plugin_config.personification_api_pools[0]["api_key"] == "auth-bound-secret"


def test_config_provider_urls_headers_and_proxy_credentials_are_redacted(
    _runtime_context  # noqa: ANN001
) -> None:
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "openai",
            "api_url": (
                "https://alice:url-password@api.example/token/"
                "0123456789abcdef0123456789abcdef?sig=signed-secret&code=oauth-secret"
                "&access_token=access-secret&refresh_token=refresh-secret"
                "&client_secret=client-secret&X-Amz-Signature=amazon-signature"
            ),
            "api_key": "provider-secret",
            "headers": {"X-Custom-Credential": "header-secret"},
            "proxy": "http://bob:proxy-password@proxy.example:8080",
            "model": "model-a",
        }
    ]
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.get("/personification/api/config/entries")

    assert response.status_code == 200
    for secret in (
        "alice",
        "url-password",
        "query-secret",
        "provider-secret",
        "header-secret",
        "bob",
        "proxy-password",
        "signed-secret",
        "oauth-secret",
        "0123456789abcdef0123456789abcdef",
        "access-secret",
        "refresh-secret",
        "client-secret",
        "amazon-signature",
    ):
        assert secret not in response.text


def test_sensitive_data_redacts_short_basic_without_masking_business_sig_substrings() -> None:
    sensitive_data = load_personification_module("plugin.personification.core.sensitive_data")

    sanitized = sensitive_data.sanitize_text(
        "https://host/cb?access_token=a1&X-Goog-Signature=s1 Basic YTpi"
    )
    prose = sensitive_data.sanitize_text("Basic authentication supports a basic design")
    business = sensitive_data.sanitize_object(
        {"design": "visible", "signal": "green", "assigned_count": 3, "code": "ok"}
    )

    assert "a1" not in sanitized
    assert "s1" not in sanitized
    assert "YTpi" not in sanitized
    assert prose == "Basic authentication supports a basic design"
    assert business == {"design": "visible", "signal": "green", "assigned_count": 3, "code": "ok"}


def test_provider_model_probe_restores_secret_only_from_valid_ref(
    _runtime_context, monkeypatch  # noqa: ANN001
) -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    _runtime_context.plugin_config.personification_api_pools = [
        {
            "name": "primary",
            "api_type": "openai",
            "api_url": "https://api.example/v1",
            "api_key": "probe-provider-secret",
            "model": "model-a",
        }
    ]
    captured: dict = {}

    async def _probe(provider):  # noqa: ANN001
        captured.update(provider)
        return ([{"id": "model-a", "label": "model-a"}], "https://api.example/v1/models")

    monkeypatch.setattr(config_routes, "_probe_http_models", _probe)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    listed = client.get("/personification/api/config/entries").json()
    provider = next(
        item
        for item in listed["entries"]
        if item["field_name"] == "personification_api_pools"
    )["current"][0]

    response = client.post(
        "/personification/api/config/provider-models",
        json={"provider": provider},
    )

    assert response.status_code == 200, response.text
    assert captured["api_key"] == "probe-provider-secret"
    assert "probe-provider-secret" not in response.text
