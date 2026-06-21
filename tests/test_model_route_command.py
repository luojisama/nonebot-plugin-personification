from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


admin_commands = load_personification_module("plugin.personification.handlers.persona_admin_commands")
provider_router = load_personification_module("plugin.personification.core.provider_router")


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolate_env_api_pools(monkeypatch) -> None:
    monkeypatch.setattr(provider_router, "_load_env_api_pool_config", lambda _logger: [])
    monkeypatch.setattr(provider_router, "_read_env_api_pool_raw", lambda: "")


class _Bundle:
    def __init__(self) -> None:
        self.plugin_config = SimpleNamespace(
            personification_api_pools=json.dumps(
                [
                    {
                        "name": "gemini_cli_primary",
                        "api_type": "gemini_cli",
                        "model": "gemini-3-flash-preview",
                        "auth_path": "~/.gemini/oauth_creds.json",
                        "priority": 1,
                        "enabled": True,
                        "supports_native_search": True,
                    },
                    {
                        "name": "codex_primary",
                        "api_type": "openai_codex",
                        "model": "gpt-5.4-mini",
                        "auth_path": "~/.codex/auth.json",
                        "priority": 2,
                        "enabled": True,
                        "supports_native_search": True,
                    },
                ]
            ),
            personification_model="gpt-4o-mini",
            personification_lite_model="",
            personification_vision_fallback_model="",
            personification_labeler_model="",
            personification_model_overrides={"agent": "gpt-5.4-mini"},
            personification_codex_auth_path="~/.codex/auth.json",
            personification_gemini_cli_auth_path="~/.gemini/oauth_creds.json",
            personification_gemini_cli_project="",
            personification_antigravity_cli_auth_path="~/.gemini/antigravity-cli/oauth_creds.json",
            personification_antigravity_cli_project="agy-project",
            personification_claude_code_auth_path="~/.claude/.credentials.json",
        )
        self.save_count = 0
        self.reload_count = 0

    def get_configured_api_providers(self):  # noqa: ANN201
        return provider_router.get_configured_api_providers(self.plugin_config, _Logger())

    def save_plugin_runtime_config(self) -> None:
        self.save_count += 1

    def reload_runtime_services(self) -> None:
        self.reload_count += 1


def test_model_route_command_reorders_provider_pool_and_clears_model_overrides() -> None:
    bundle = _Bundle()

    result = admin_commands.handle_model_command(bundle, tokens=["路由", "codex_primary"])

    pools = bundle.plugin_config.personification_api_pools
    assert pools[0]["name"] == "codex_primary"
    assert pools[0]["priority"] == 1
    assert pools[1]["name"] == "gemini_cli_primary"
    assert bundle.plugin_config.personification_model_overrides == {}
    assert bundle.save_count == 1
    assert bundle.reload_count == 1
    assert "已热切主 provider" in result


def test_model_route_command_can_create_gemini_cli_pool() -> None:
    bundle = _Bundle()
    bundle.plugin_config.personification_api_pools = None

    result = admin_commands.handle_model_command(
        bundle,
        tokens=["路由", "gemini_cli", "gemini-3-flash-preview"],
    )

    pools = bundle.plugin_config.personification_api_pools
    assert pools[0]["api_type"] == "gemini_cli"
    assert pools[0]["model"] == "gemini-3-flash-preview"
    assert pools[0]["auth_path"] == "~/.gemini/oauth_creds.json"
    assert bundle.plugin_config.personification_model_overrides == {}
    assert bundle.save_count == 1
    assert bundle.reload_count == 1
    assert "gemini_cli_primary" in result


def test_model_route_command_can_create_antigravity_cli_pool() -> None:
    bundle = _Bundle()
    bundle.plugin_config.personification_api_pools = None

    result = admin_commands.handle_model_command(
        bundle,
        tokens=["路由", "antigravity_cli", "gemini-3-flash-preview"],
    )

    pools = bundle.plugin_config.personification_api_pools
    assert pools[0]["api_type"] == "antigravity_cli"
    assert pools[0]["model"] == "gemini-3-flash-preview"
    assert pools[0]["auth_path"] == "~/.gemini/antigravity-cli/oauth_creds.json"
    assert pools[0]["project"] == "agy-project"
    assert "antigravity_cli_primary" in result


def test_model_status_shows_rate_limit_cooldown() -> None:
    bundle = _Bundle()

    class _Response:
        status_code = 429
        headers = {"Retry-After": "900"}

    class _RateLimitError(Exception):
        response = _Response()

    provider_router.PROVIDER_FAILURE_STATE.clear()
    try:
        provider_router._mark_provider_failure("gemini_cli_primary", _RateLimitError("429 Too Many Requests"))

        result = admin_commands.handle_model_command(bundle, tokens=[])

        assert "gemini_cli_primary" in result
        assert "限流冷却" in result
    finally:
        provider_router.PROVIDER_FAILURE_STATE.clear()
