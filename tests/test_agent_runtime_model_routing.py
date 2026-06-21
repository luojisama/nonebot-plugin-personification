from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

agent_runtime = load_personification_module("plugin.personification.core.services.agent_runtime")


class _Logger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []
        self.info_messages: list[str] = []

    def warning(self, message: str) -> None:
        self.warning_messages.append(str(message))

    def info(self, message: str) -> None:
        self.info_messages.append(str(message))


def test_lite_tool_caller_uses_lite_model_when_strict_mode_disabled(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict] = []
    default_caller = object()
    lite_caller = object()

    def _fake_build_routed_tool_caller(**kwargs):  # noqa: ANN001
        calls.append(dict(kwargs))
        return lite_caller

    monkeypatch.setattr(agent_runtime, "build_routed_tool_caller", _fake_build_routed_tool_caller)
    cfg = SimpleNamespace(
        personification_strict_main_model=False,
        personification_model_overrides={},
        personification_lite_model="gpt-5-mini",
    )

    result = agent_runtime._build_lite_tool_caller(cfg, _Logger(), default_caller)

    assert result is lite_caller
    assert calls and calls[0]["model_override"] == "gpt-5-mini"


def test_lite_tool_caller_reuses_main_caller_when_strict_mode_enabled(monkeypatch) -> None:  # noqa: ANN001
    def _should_not_build(**_kwargs):  # noqa: ANN001
        raise AssertionError("strict main mode must not build a lite caller")

    monkeypatch.setattr(agent_runtime, "build_routed_tool_caller", _should_not_build)
    default_caller = object()
    logger = _Logger()
    cfg = SimpleNamespace(
        personification_strict_main_model=True,
        personification_model_overrides={},
        personification_lite_model="gpt-5-mini",
    )

    result = agent_runtime._build_lite_tool_caller(cfg, logger, default_caller)

    assert result is default_caller
    assert any("将被忽略" in message for message in logger.warning_messages)
