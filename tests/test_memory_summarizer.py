from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
data_store = load_personification_module("plugin.personification.core.data_store")
memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
memory_summarizer = load_personification_module("plugin.personification.core.memory_summarizer")


def _init_store(tmp_path: Path):
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=True,
        personification_memory_recall_top_k=8,
        personification_memory_summarizer_enabled=True,
    )
    data_store.init_data_store(cfg)
    db.init_db_sync(tmp_path)
    store = memory_store_mod.MemoryStore(
        plugin_config=cfg,
        logger=SimpleNamespace(warning=lambda *_a, **_k: None),
    )
    store.initialize()
    return cfg, store


def test_summarize_session_segment(tmp_path: Path) -> None:
    _cfg, store = _init_store(tmp_path)

    class _Caller:
        async def chat_with_tools(self, **_kwargs):
            return SimpleNamespace(
                content="今天群里主要聊了游戏和晚饭。大家整体氛围轻松。",
                usage={},
            )

    messages = [
        {"text": "今天吃啥", "user_id": "u1", "created_at": 1000.0},
        {"text": "随便吧", "user_id": "u2", "created_at": 1001.0},
        {"text": "打游戏去", "user_id": "u1", "created_at": 1002.0},
    ]

    summary = asyncio.run(
        memory_summarizer.summarize_session_segment(
            tool_caller=_Caller(),
            group_id="g1",
            messages=messages,
        )
    )

    assert summary is not None
    assert "游戏" in summary


def test_summarize_returns_none_for_empty_input(tmp_path: Path) -> None:
    _cfg, store = _init_store(tmp_path)

    summary = asyncio.run(
        memory_summarizer.summarize_session_segment(
            tool_caller=None,
            group_id="g1",
            messages=[],
        )
    )

    assert summary is None


def test_register_jobs_disabled(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_summarizer_enabled=False,
    )
    calls: list[str] = []

    class _Scheduler:
        def add_job(self, *args, **kwargs):
            calls.append(kwargs.get("id", ""))

    memory_summarizer.register_memory_summarizer_jobs(
        scheduler=_Scheduler(),
        plugin_config=cfg,
        memory_store=None,
        tool_caller=None,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )

    assert len(calls) == 0


def test_register_jobs_enabled(tmp_path: Path) -> None:
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_summarizer_enabled=True,
    )
    calls: list[str] = []

    class _Scheduler:
        def add_job(self, *args, **kwargs):
            calls.append(kwargs.get("id", ""))

    memory_summarizer.register_memory_summarizer_jobs(
        scheduler=_Scheduler(),
        plugin_config=cfg,
        memory_store=None,
        tool_caller=None,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
    )

    assert "personification_session_summarizer" in calls
    assert "personification_daily_summarizer" in calls
