from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from plugin.personification.core import provider_router
from plugin.personification.skills.skillpacks.tool_caller.scripts.impl import ToolCallerResponse


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, message, *_args, **_kwargs) -> None:  # noqa: ANN001
        self.warnings.append(str(message))

    def error(self, message, *_args, **_kwargs) -> None:  # noqa: ANN001
        self.warnings.append(str(message))


class _HttpError(RuntimeError):
    def __init__(self, status_code: int, *, retry_after: str = "") -> None:
        super().__init__(f"request failed at https://secret.example/v1/models/private: HTTP {status_code}")
        self.response = SimpleNamespace(
            status_code=status_code,
            headers={"Retry-After": retry_after} if retry_after else {},
        )


def _provider(name: str, *, attempts: int = 5) -> dict:
    return {
        "name": name,
        "api_type": "openai",
        "model": "test-model",
        "max_retries": attempts,
    }


def _success() -> ToolCallerResponse:
    return ToolCallerResponse("stop", "ok", [], {})


def test_provider_defaults_and_explicit_overrides() -> None:
    base = {
        "name": "main",
        "api_type": "openai",
        "api_url": "https://example.test/v1",
        "api_key": "secret",
        "model": "model",
    }

    defaulted = provider_router.parse_api_pool_config([base])[0]
    overridden = provider_router.parse_api_pool_config([{**base, "timeout": 90, "max_retries": 3}])[0]

    assert defaulted["timeout"] == 200
    assert defaulted["max_retries"] == 5
    assert overridden["timeout"] == 90
    assert overridden["max_retries"] == 3


def test_gemini_caller_receives_provider_timeout() -> None:
    caller = provider_router._build_provider_caller(
        {
            "name": "gemini",
            "api_type": "gemini",
            "api_url": "https://example.test/v1beta",
            "api_key": "secret",
            "model": "gemini-test",
            "timeout": 73,
        },
        SimpleNamespace(personification_thinking_mode="none"),
    )

    assert caller.timeout == 73


def test_transient_failure_retries_up_to_five_attempts(monkeypatch) -> None:  # noqa: ANN001
    calls = 0
    delays: list[float] = []

    async def _call(*_args, **_kwargs):  # noqa: ANN202
        nonlocal calls
        calls += 1
        if calls < 5:
            raise _HttpError(503)
        return _success()

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router.asyncio, "sleep", _sleep)
    provider_router.PROVIDER_FAILURE_STATE.clear()

    response, errors, attempts, _ = asyncio.run(
        provider_router._try_provider_chain(
            [_provider("primary")],
            messages=[],
            plugin_config=SimpleNamespace(),
            logger=_Logger(),
        )
    )

    assert response is not None and response.content == "ok"
    assert calls == 5
    assert delays == [1.0, 2.0, 4.0, 8.0]
    assert len(errors) == 4
    assert len(attempts) == 4
    assert all(item["code"] == "provider_call_failed" for item in attempts)
    assert "primary" not in provider_router.PROVIDER_FAILURE_STATE


def test_timeout_retries_instead_of_skipping_remaining_attempts(monkeypatch) -> None:  # noqa: ANN001
    calls = 0
    delays: list[float] = []

    async def _call(*_args, **_kwargs):  # noqa: ANN202
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError()
        return _success()

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router.asyncio, "sleep", _sleep)

    response, _, attempts, _ = asyncio.run(
        provider_router._try_provider_chain(
            [_provider("slow")],
            messages=[],
            plugin_config=SimpleNamespace(),
            logger=_Logger(),
        )
    )

    assert response is not None
    assert calls == 2
    assert delays == [1.0]
    assert attempts[0]["request_count"] == 1


def test_http_400_does_not_retry_and_switches_provider(monkeypatch) -> None:  # noqa: ANN001
    calls = {"bad": 0, "backup": 0}
    delays: list[float] = []
    logger = _Logger()

    async def _call(provider, *_args, **_kwargs):  # noqa: ANN001, ANN202
        calls[provider["name"]] += 1
        if provider["name"] == "bad":
            raise _HttpError(400)
        return _success()

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(provider_router, "_call_provider_once", _call)
    monkeypatch.setattr(provider_router.asyncio, "sleep", _sleep)
    provider_router.PROVIDER_FAILURE_STATE.clear()

    response, errors, attempts, _ = asyncio.run(
        provider_router._try_provider_chain(
            [_provider("bad"), _provider("backup")],
            messages=[],
            plugin_config=SimpleNamespace(),
            logger=logger,
        )
    )

    assert response is not None
    assert calls == {"bad": 1, "backup": 1}
    assert delays == []
    assert any("status=400" in item for item in errors)
    assert attempts[0]["code"] == "provider_request_rejected"
    assert attempts[0]["status_code"] == 400
    assert all("secret.example" not in item for item in errors)
    assert all("secret.example" not in item for item in logger.warnings)
    assert "secret.example" not in provider_router.PROVIDER_FAILURE_STATE["bad"]["last_error"]


@pytest.mark.parametrize("status", [408, 409, 425, 429, 500, 503])
def test_retryable_http_statuses(status: int) -> None:
    assert provider_router._is_retryable_provider_error(_HttpError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_non_retryable_http_statuses(status: int) -> None:
    assert provider_router._is_retryable_provider_error(_HttpError(status)) is False


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (400, "provider_request_rejected"),
        (401, "provider_auth_failed"),
        (403, "provider_permission_denied"),
        (404, "provider_model_unavailable"),
        (422, "provider_request_rejected"),
    ],
)
def test_call_ai_api_raises_structured_error_after_provider_exhaustion(
    monkeypatch,
    status: int,
    expected_code: str,
) -> None:  # noqa: ANN001
    provider = {
        **_provider("gemini", attempts=1),
        "api_type": "gemini",
        "gemini_auth_mode": "auto",
    }
    upstream_error = _HttpError(status)
    upstream_error.code = "opaque-secret-code"
    upstream_error.retryable = True
    upstream_error.auth_mode = "bearer"
    upstream_error.request_count = 2

    async def _call(*_args, **_kwargs):  # noqa: ANN202
        raise upstream_error

    monkeypatch.setattr(provider_router, "get_provider_candidates", lambda *_args: [provider])
    monkeypatch.setattr(provider_router, "_call_provider_once", _call)

    with pytest.raises(provider_router.ProviderRouteError) as caught:
        asyncio.run(provider_router.call_ai_api(
            [],
            plugin_config=SimpleNamespace(personification_fallback_enabled=False),
            logger=_Logger(),
        ))

    error = caught.value
    assert error.code == expected_code
    assert error.status_code == status
    assert error.retryable is False
    assert len(error.route_attempts) == 1
    assert error.route_attempts[0]["auth_mode"] == "bearer"
    assert error.route_attempts[0]["request_count"] == 2
    assert "opaque-secret-code" not in str(error.route_attempts)
    assert "secret.example" not in str(error.route_attempts)


def test_call_ai_api_without_provider_is_structured(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(provider_router, "get_provider_candidates", lambda *_args: [])

    with pytest.raises(provider_router.ProviderRouteError) as caught:
        asyncio.run(provider_router.call_ai_api(
            [],
            plugin_config=SimpleNamespace(personification_fallback_enabled=False),
            logger=_Logger(),
        ))

    assert caught.value.code == "provider_caller_unavailable"
    assert caught.value.retryable is False


def test_network_error_type_is_retryable_without_message_matching() -> None:
    error = httpx.ReadError("read failed")

    assert provider_router._is_retryable_provider_error(error) is True
    assert provider_router._provider_failure_code(error) == "provider_network_failed"


def test_explicit_retryable_metadata_is_preserved() -> None:
    error = RuntimeError("opaque failure")
    error.retryable = True

    assert provider_router._is_retryable_provider_error(error) is True


def test_wrapped_timeout_is_canonical_and_retryable() -> None:
    error = RuntimeError("outer failure")
    error.__cause__ = TimeoutError("upstream timeout")

    assert provider_router._is_retryable_provider_error(error) is True
    assert provider_router._provider_failure_code(error) == "provider_timeout"


def test_model_candidate_unavailable_remains_retryable() -> None:
    error = _HttpError(404)
    error.code = "provider_model_candidate_unavailable"
    error.retryable = True

    assert provider_router._is_retryable_provider_error(error) is True
    assert provider_router._provider_failure_code(error) == "provider_model_candidate_unavailable"


def test_mixed_vision_unavailable_preserves_visual_fallback_signal(monkeypatch) -> None:  # noqa: ANN001
    providers = [_provider("vision", attempts=1), _provider("broken", attempts=1)]

    async def _call(provider, *_args, **_kwargs):  # noqa: ANN001, ANN202
        if provider["name"] == "vision":
            return ToolCallerResponse("stop", "", [], {}, vision_unavailable=True)
        raise _HttpError(503)

    monkeypatch.setattr(provider_router, "get_provider_candidates", lambda *_args: providers)
    monkeypatch.setattr(provider_router, "_call_provider_once", _call)

    response = asyncio.run(provider_router.call_ai_api(
        [{"role": "user", "content": "image"}],
        plugin_config=SimpleNamespace(personification_fallback_enabled=False),
        logger=_Logger(),
    ))

    assert response.vision_unavailable is True


def test_rate_limit_retry_after_is_capped() -> None:
    assert provider_router._provider_retry_delay(_HttpError(429, retry_after="900"), 0) == 30.0
