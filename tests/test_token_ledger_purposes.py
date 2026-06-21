"""Token ledger 拦截覆盖：各 LLM 调用入口要把 purpose（user_persona/group_style/...）正确写入。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _tmp_data(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    data_store.init_data_store(cfg)
    return tmp_path


def _fake_response(prompt_tokens: int = 10, completion_tokens: int = 5, model: str = "test"):
    return SimpleNamespace(
        content="ok",
        finish_reason="stop",
        tool_calls=[],
        raw={},
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens},
        model_used=model,
    )


def test_record_response_usage_reads_context(_tmp_data) -> None:
    ledger = load_personification_module("plugin.personification.core.token_ledger")
    llm_ctx = load_personification_module("plugin.personification.core.llm_context")

    token = llm_ctx.set_llm_context(purpose="user_persona", user_id="10086")
    try:
        ledger.record_response_usage(_fake_response(prompt_tokens=100, completion_tokens=20))
    finally:
        llm_ctx.reset_llm_context(token)

    summary = ledger.query_summary(window="day")
    purposes = {row["purpose"] for row in summary.get("by_purpose", [])}
    matched = [p for p in purposes if "user_persona" in p]
    assert matched, f"应有 user_persona 记录，实际 purposes={purposes}"


def test_record_response_usage_skips_zero_usage(_tmp_data) -> None:
    ledger = load_personification_module("plugin.personification.core.token_ledger")
    ledger.record_response_usage(_fake_response(prompt_tokens=0, completion_tokens=0))
    summary = ledger.query_summary(window="day")
    total = summary.get("total", {})
    assert int(total.get("total_tokens", 0)) == 0


def test_record_response_usage_falls_back_to_unknown_purpose(_tmp_data) -> None:
    ledger = load_personification_module("plugin.personification.core.token_ledger")
    # 不 set context，purpose 应该 fallback 到 direct_call
    ledger.record_response_usage(_fake_response(prompt_tokens=11, completion_tokens=7))
    summary = ledger.query_summary(window="day")
    purposes = {row["purpose"] for row in summary.get("by_purpose", [])}
    matched = [p for p in purposes if "direct_call" in p]
    assert matched, f"无 context 时应记 direct_call，实际 purposes={purposes}"


def test_persona_service_records_token_usage(_tmp_data) -> None:
    persona_service = load_personification_module("plugin.personification.core.persona_service")
    ledger = load_personification_module("plugin.personification.core.token_ledger")

    class _FakeCaller:
        model = "fake-model"

        async def chat_with_tools(self, **_):
            return _fake_response(prompt_tokens=50, completion_tokens=30)

    store = persona_service.PersonaStore(
        data_dir=_tmp_data,
        tool_caller=_FakeCaller(),
        history_max=10,
        logger=SimpleNamespace(warning=lambda *_a, **_k: None),
    )

    async def _run() -> str | None:
        return await store._call_persona_llm(
            messages=["test msg"],
            previous=None,
            user_id="98765",
        )

    result = asyncio.run(_run())
    assert result == "ok"
    summary = ledger.query_summary(window="day")
    purposes = {row["purpose"] for row in summary.get("by_purpose", [])}
    assert any("user_persona" in p for p in purposes), purposes


def test_group_knowledge_records_token_usage(_tmp_data) -> None:
    group_knowledge = load_personification_module("plugin.personification.core.group_knowledge")
    ledger = load_personification_module("plugin.personification.core.token_ledger")

    class _FakeCaller:
        async def chat_with_tools(self, **_):
            # 返回有效 JSON 数组让 build_group_knowledge 通过解析
            r = _fake_response(prompt_tokens=200, completion_tokens=40)
            r.content = '[{"term":"肉鸽","definition":"群内常用词"}]'
            return r

    class _FakeMemoryStore:
        def write_memory_item(self, _payload):
            pass

    async def _run() -> int:
        return await group_knowledge.build_group_knowledge(
            tool_caller=_FakeCaller(),
            memory_store=_FakeMemoryStore(),
            group_id="g111",
            chat_summary="some chat",
        )

    saved = asyncio.run(_run())
    assert saved == 1
    summary = ledger.query_summary(window="day")
    by_group = {row["group_id"] for row in summary.get("by_group", [])}
    assert "g111" in by_group, f"应按 group_id 记账，实际 by_group={by_group}"
    purposes = {row["purpose"] for row in summary.get("by_purpose", [])}
    assert any("group_knowledge" in p for p in purposes), purposes


def test_group_style_records_token_usage(_tmp_data) -> None:
    gsa = load_personification_module("plugin.personification.core.group_style_autobuild")
    ledger = load_personification_module("plugin.personification.core.token_ledger")

    class _FakeCaller:
        async def chat_with_tools(self, **_):
            r = _fake_response(prompt_tokens=180, completion_tokens=60)
            r.content = '{"tone":"轻松","pace":"中速","catchphrases":["梗"],"taboos":[],"typical_length":"短"}'
            return r

    captured: list[dict] = []

    class _FakeMemoryStore:
        def write_memory_item(self, payload):
            captured.append(payload)

    async def _run():
        return await gsa.build_group_style(
            tool_caller=_FakeCaller(),
            memory_store=_FakeMemoryStore(),
            group_id="g222",
            chat_summary="chat content",
        )

    result = asyncio.run(_run())
    assert result, "应成功生成 snapshot"
    summary = ledger.query_summary(window="day")
    purposes = {row["purpose"] for row in summary.get("by_purpose", [])}
    assert any("group_style" in p for p in purposes), purposes
