from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module

prompt_loader = load_personification_module("plugin.personification.core.prompt_loader")
context_policy = load_personification_module("plugin.personification.core.context_policy")


class _FakeLogger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.error_messages: list[str] = []
        self.warning_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def error(self, message: str) -> None:
        self.error_messages.append(message)

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


def _config(**overrides) -> SimpleNamespace:  # noqa: ANN003
    values = {
        "personification_agent_enabled": False,
        "personification_agent_max_steps": 10,
        "personification_prompt_path": "",
        "personification_system_path": "",
        "personification_system_prompt": "你是群友",
        "personification_core_values_enabled": True,
        "personification_core_values_prompt": "尊重生命、公共安全和法律责任，别把危险或违法行为说成值得同情。",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_load_prompt_reuses_yaml_cache_and_pick_ack_phrase(monkeypatch) -> None:  # noqa: ANN001
    temp_dir = Path(__file__).resolve().parent / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = temp_dir / f"persona_{time.time_ns()}.yaml"
    yaml_path.write_text(
        "system: 你是群友\nack_phrases:\n  - 查下~\n  - 等等\n",
        encoding="utf-8",
    )
    logger = _FakeLogger()
    plugin_config = SimpleNamespace(
        personification_agent_enabled=False,
        personification_prompt_path=str(yaml_path),
        personification_system_path="",
        personification_system_prompt="",
    )
    prompt_loader.clear_yaml_prompt_cache()
    monkeypatch.setattr(prompt_loader.random, "choice", lambda items: items[0])

    first = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger, group_id="123")
    second = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger, group_id="123")
    ack_phrase = prompt_loader.pick_ack_phrase(plugin_config, lambda _gid: {}, logger, group_id="123")

    assert first == second
    assert isinstance(first, dict)
    assert first["system"].startswith("你是群友")
    assert first["system"].count(context_policy.PROMPT_INJECTION_GUARD_MARKER) == 1
    assert ack_phrase == "查下~"
    assert len(logger.info_messages) == 1
    yaml_path.unlink(missing_ok=True)


def test_core_values_are_appended_to_string_prompt_when_agent_disabled() -> None:
    logger = _FakeLogger()
    plugin_config = _config(personification_agent_enabled=False)

    loaded = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger)

    assert isinstance(loaded, str)
    assert "你是群友" in loaded
    assert prompt_loader.CORE_VALUES_MARKER in loaded
    assert "公共安全" in loaded
    assert prompt_loader.AGENT_GUIDANCE_MARKER not in loaded


def test_core_values_are_appended_to_yaml_system_without_duplicate() -> None:
    temp_dir = Path(__file__).resolve().parent / ".tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    yaml_path = temp_dir / f"persona_core_values_{time.time_ns()}.yaml"
    yaml_path.write_text(
        "system: 你是群友\ninput: '{message}'\n",
        encoding="utf-8",
    )
    logger = _FakeLogger()
    plugin_config = _config(
        personification_agent_enabled=True,
        personification_prompt_path=str(yaml_path),
    )
    prompt_loader.clear_yaml_prompt_cache()

    first = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger)
    second = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger)

    assert first == second
    assert isinstance(first, dict)
    system_prompt = first["system"]
    assert system_prompt.count(prompt_loader.CORE_VALUES_MARKER) == 1
    assert system_prompt.count(prompt_loader.AGENT_GUIDANCE_MARKER) == 1
    assert system_prompt.count(context_policy.PROMPT_INJECTION_GUARD_MARKER) == 1
    assert "你是群友" in system_prompt
    yaml_path.unlink(missing_ok=True)


def test_core_values_can_be_disabled() -> None:
    logger = _FakeLogger()
    plugin_config = _config(personification_core_values_enabled=False)

    loaded = prompt_loader.load_prompt(plugin_config, lambda _gid: {}, logger)

    assert isinstance(loaded, str)
    assert loaded.startswith("你是群友")
    assert loaded.count(context_policy.PROMPT_INJECTION_GUARD_MARKER) == 1
