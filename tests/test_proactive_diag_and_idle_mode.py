"""J2 主动诊断 + J4 水群两阶段模式决策的测试。"""
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


# ---- J2 主动诊断 ----


def test_diag_record_and_query(_tmp_data) -> None:
    diag = load_personification_module("plugin.personification.core.proactive_diagnostics")
    diag.record(scope="private", outcome=diag.SKIP_DAILY_LIMIT, target="10001", detail={"daily_limit": 3})
    diag.record(scope="private", outcome=diag.OUTCOME_SENT, target="20002")
    diag.record(scope="group_idle", outcome=diag.SKIP_QUIET_HOUR, target="g111")

    rows = diag.query_recent(limit=10)
    assert len(rows) == 3
    assert rows[0]["scope"] in {"private", "group_idle"}

    # 按 scope 筛
    pri_rows = diag.query_recent(scope="private")
    assert all(r["scope"] == "private" for r in pri_rows)
    assert len(pri_rows) == 2


def test_diag_skip_reason_stats(_tmp_data) -> None:
    diag = load_personification_module("plugin.personification.core.proactive_diagnostics")
    for _ in range(5):
        diag.record(scope="private", outcome=diag.SKIP_PROBABILITY)
    for _ in range(2):
        diag.record(scope="private", outcome=diag.OUTCOME_SENT)
    stats = diag.query_skip_reason_stats(scope="private")
    assert stats.get("skip_probability") == 5
    assert stats.get("sent") == 2


def test_diag_next_eligible(_tmp_data) -> None:
    diag = load_personification_module("plugin.personification.core.proactive_diagnostics")
    import time as _t
    future = _t.time() + 3600
    diag.record(scope="private", outcome=diag.SKIP_COOLDOWN, target="A", next_eligible_at=future)
    rows = diag.query_next_eligible(scope="private")
    assert any(r["target"] == "A" for r in rows)


# ---- J4 主动水群模式决策 ----


def _make_fake_call_ai_api(response: str):
    async def _fake(messages, **_):
        return response
    return _fake


def test_decide_idle_mode_parses_text_json() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api('{"mode":"text","mood":"日常"}')
    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call,
        topic="周末干嘛",
        group_style="轻松",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "text"
    assert mood == "日常"


def test_decide_idle_mode_parses_sticker_json() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api('{"mode":"sticker","mood":"困倦"}')
    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call, topic="夜深了", group_style="轻松",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "sticker"
    assert mood == "困倦"


def test_decide_idle_mode_combo() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api('{"mode":"combo","mood":"兴奋"}')
    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call, topic="抢到票了", group_style="轻松",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "combo"


def test_decide_idle_mode_strips_markdown_codeblock() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api('```json\n{"mode":"sticker","mood":"调侃"}\n```')
    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call, topic="?", group_style="",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "sticker"
    assert mood == "调侃"


def test_decide_idle_mode_invalid_mode_falls_back_to_text() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api('{"mode":"weird_mode","mood":"x"}')
    mode, _ = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call, topic="?", group_style="",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "text"


def test_decide_idle_mode_garbage_json_returns_text() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")
    call = _make_fake_call_ai_api("garbage non-json")
    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=call, topic="?", group_style="",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "text"
    assert mood == ""


def test_decide_idle_mode_llm_exception_returns_text() -> None:
    pf = load_personification_module("plugin.personification.flows.proactive_flow")

    async def _fail(_messages, **_kwargs):
        raise RuntimeError("LLM unavailable")

    mode, mood = asyncio.run(pf._decide_idle_output_mode(
        call_ai_api=_fail, topic="?", group_style="",
        logger=SimpleNamespace(debug=lambda *_: None, info=lambda *_: None, warning=lambda *_: None),
        group_id="g1",
    ))
    assert mode == "text"
    assert mood == ""
