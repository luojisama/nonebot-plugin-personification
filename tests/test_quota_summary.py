"""Provider 额度可视化：本地 token_ledger 按 provider 聚合 + QQ 命令格式化。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _ledger(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    return load_personification_module("plugin.personification.core.token_ledger")


def test_record_with_explicit_provider_aggregates(_ledger) -> None:
    _ledger.record_llm_call(model="claude-3-7-sonnet", prompt_tokens=100, completion_tokens=50, provider="anthropic")
    _ledger.record_llm_call(model="claude-3-7-sonnet", prompt_tokens=200, completion_tokens=80, provider="anthropic")
    _ledger.record_llm_call(model="gpt-4o", prompt_tokens=150, completion_tokens=60, provider="openai")

    out = _ledger.query_provider_summary("month")
    providers = {p["provider"]: p for p in out["providers"]}
    assert "anthropic" in providers
    assert providers["anthropic"]["total_tokens"] == 430
    assert "openai" in providers
    assert providers["openai"]["total_tokens"] == 210


def test_provider_inferred_from_model_name(_ledger) -> None:
    """无显式 provider 时应能从 model 名推导。"""
    _ledger.record_llm_call(model="claude-3-sonnet", prompt_tokens=50, completion_tokens=20)
    _ledger.record_llm_call(model="gemini-2.5-pro", prompt_tokens=40, completion_tokens=10)
    _ledger.record_llm_call(model="gpt-4o-mini", prompt_tokens=30, completion_tokens=5)

    out = _ledger.query_provider_summary("month")
    providers = {p["provider"]: p for p in out["providers"]}
    assert "anthropic" in providers
    assert "gemini" in providers
    assert "openai" in providers


def test_codex_provider_explicit_overrides_gpt_in_model_name(_ledger) -> None:
    """Codex 用 gpt-* 模型，应被 explicit provider='codex' 覆盖。"""
    _ledger.record_llm_call(model="gpt-5-codex", prompt_tokens=80, completion_tokens=30, provider="codex")
    out = _ledger.query_provider_summary("month")
    providers = {p["provider"]: p for p in out["providers"]}
    assert "codex" in providers
    assert providers["codex"]["total_tokens"] == 110


def test_unknown_provider_falls_back_to_unknown(_ledger) -> None:
    """无 provider 且 model 名不识别时，归到 unknown。"""
    _ledger.record_llm_call(model="local-mistral-7b", prompt_tokens=10, completion_tokens=5)
    out = _ledger.query_provider_summary("month")
    providers = {p["provider"]: p for p in out["providers"]}
    assert "unknown" in providers


def test_qq_command_renders_full_format(_ledger) -> None:
    """`拟人额度` 命令应返回 4 行 provider + 限额。"""
    _ledger.record_llm_call(model="claude", prompt_tokens=1_200_000, completion_tokens=300_000, provider="anthropic")
    _ledger.record_llm_call(model="gpt-4o", prompt_tokens=500_000, completion_tokens=100_000, provider="openai")

    cfg = SimpleNamespace(
        personification_quota_anthropic_monthly_tokens=5_000_000,
        personification_quota_openai_monthly_tokens=10_000_000,
        personification_quota_gemini_cli_monthly_tokens=0,
        personification_quota_codex_monthly_tokens=0,
    )

    captured: list[str] = []

    class _Matcher:
        async def finish(self, text):
            captured.append(text)
            raise StopAsyncIteration  # 模拟 matcher.finish 中止

    cmd = load_personification_module("plugin.personification.handlers.runtime_commands")
    try:
        asyncio.run(cmd.handle_quota_command(_Matcher(), plugin_config=cfg))
    except StopAsyncIteration:
        pass
    assert captured, "应当至少调一次 finish"
    text = captured[0]
    assert "Anthropic" in text
    assert "1.50M" in text  # 1.2M prompt + 0.3M completion = 1.5M total
    assert "5.00M" in text  # 上限
    assert "Gemini/Antigravity CLI" in text
    assert "∞" in text  # 未设上限
    assert "本地记账" in text


def test_provider_summary_empty_returns_zero_buckets(_ledger) -> None:
    out = _ledger.query_provider_summary("month")
    assert isinstance(out["providers"], list)
    # 即使无任何调用，结构应当合法（空列表）
