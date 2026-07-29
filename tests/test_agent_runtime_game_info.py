from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module


agent_runtime = load_personification_module("plugin.personification.core.services.agent_runtime")


class _Logger:
    def warning(self, _message: str) -> None:
        pass

    def debug(self, _message: str) -> None:
        pass


def test_legacy_registry_exposes_game_info_by_default() -> None:
    config = SimpleNamespace(
        personification_use_skillpacks=False,
        personification_game_info_enabled=True,
        personification_memory_enabled=False,
        personification_tool_web_fetch_enabled=False,
        personification_60s_enabled=False,
        personification_timezone="Asia/Shanghai",
        personification_sticker_path="__missing_stickers__",
    )
    registry = agent_runtime.build_agent_tool_registry(
        plugin_config=config,
        logger=_Logger(),
        get_now=lambda: None,
    )

    assert "game_info" in {tool.name for tool in registry.active()}
    assert registry.get("game_info").metadata["source_kind"] == "builtin"


def test_legacy_registry_exposes_tech_news_by_default() -> None:
    config = SimpleNamespace(
        personification_use_skillpacks=False,
        personification_game_info_enabled=False,
        personification_memory_enabled=False,
        personification_tool_web_fetch_enabled=False,
        personification_60s_enabled=True,
        personification_timezone="Asia/Shanghai",
        personification_sticker_path="__missing_stickers__",
    )

    registry = agent_runtime.build_agent_tool_registry(
        plugin_config=config,
        logger=_Logger(),
        get_now=lambda: None,
    )

    assert registry.get("get_tech_news") is not None
    assert registry.get("get_tech_news").metadata["source_kind"] == "builtin"


def test_skill_runtime_exposes_configured_api_pool_reader(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _capture_runtime(*, runtime, registry) -> None:  # noqa: ANN001
        del registry
        captured["runtime"] = runtime

    monkeypatch.setattr(agent_runtime, "load_builtin_skillpacks_sync", _capture_runtime)
    config = SimpleNamespace(
        personification_use_skillpacks=True,
        personification_api_type="openai",
        personification_api_url="",
        personification_api_key="",
        personification_model="",
        personification_api_pools=[
            {
                "name": "gemini_pool",
                "api_type": "gemini",
                "api_url": "https://gemini.example/v1beta",
                "api_key": "pool-secret",
                "model": "gemini-test",
                "gemini_auth_mode": "bearer",
            }
        ],
    )

    agent_runtime.build_agent_tool_registry(
        plugin_config=config,
        logger=_Logger(),
        get_now=lambda: None,
    )

    runtime = captured["runtime"]
    providers = runtime.get_configured_api_providers()
    assert providers[0]["name"] == "gemini_pool"
    assert providers[0]["gemini_auth_mode"] == "bearer"
