from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module

prompt_loader = load_personification_module("plugin.personification.core.prompt_loader")


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
    assert first["system"] == "你是群友"
    assert ack_phrase == "查下~"
    assert len(logger.info_messages) == 1
    yaml_path.unlink(missing_ok=True)
