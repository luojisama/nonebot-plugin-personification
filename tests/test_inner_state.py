from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


inner_state = load_personification_module("plugin.personification.agent.inner_state")


def test_normalize_inner_state_compacts_llm_sentence_mood_and_pending_objects() -> None:
    state = inner_state.normalize_inner_state(
        {
            "mood": "明天大概会先带着一点疲惫和吐槽欲，但也还想靠摸鱼慢慢回血。",
            "energy": "中等偏上",
            "pending_thoughts": [{"thought": "回头确认收藏表情接口"}, {"text": "看看 WebUI pending 展示"}],
            "relation_warmth": {"u1": 2, "u2": "-2", "bad": "x"},
        }
    )

    assert state["mood"] == "疲惫"
    assert state["energy"] == "中"
    assert state["pending_thoughts"] == [
        {"thought": "回头确认收藏表情接口"},
        {"thought": "看看 WebUI pending 展示"},
    ]
    assert state["relation_warmth"] == {"u1": 1.0, "u2": -1.0}


def test_merge_state_keeps_mood_short_and_does_not_concatenate(monkeypatch) -> None:  # noqa: ANN001
    class _NoonDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            current = datetime.now(tz)
            return cls(current.year, current.month, current.day, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(inner_state, "datetime", _NoonDateTime)
    current = {
        "mood": "平静",
        "energy": "正常",
        "updated_at": (_NoonDateTime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    merged = inner_state._merge_state(
        current,
        {
            "mood": "有点无语但又想吐槽",
            "energy": "高",
            "pending_thoughts": ["之后看一下日志"],
        },
    )

    assert merged["mood"] == "无语"
    assert "但有些" not in merged["mood"]
    assert merged["energy"] == "高"
    assert merged["pending_thoughts"] == [{"thought": "之后看一下日志"}]


def test_inner_state_llm_update_does_not_block_state_reads(monkeypatch) -> None:  # noqa: ANN001
    class _Store:
        def __init__(self) -> None:
            self.state = copy.deepcopy(inner_state.DEFAULT_STATE)
            self.state["updated_at"] = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            self.lock = asyncio.Lock()

        async def load(self, _name: str):  # noqa: ANN001
            async with self.lock:
                return copy.deepcopy(self.state)

        async def mutate(self, _name: str, mutator):  # noqa: ANN001
            async with self.lock:
                self.state = mutator(copy.deepcopy(self.state))
                return copy.deepcopy(self.state)

    class _Caller:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_with_tools(self, **_kwargs):  # noqa: ANN001
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(content=json.dumps({"mood": "无语"}, ensure_ascii=False))

    class _Logger:
        def warning(self, _message: str) -> None:
            return None

    async def _run() -> None:
        store = _Store()
        caller = _Caller()
        monkeypatch.setattr(inner_state, "_get_data_store", lambda: store)
        task = asyncio.create_task(
            inner_state.update_inner_state_after_chat(
                Path("."),
                caller,
                "刚聊完一轮",
                {},
                "none",
                _Logger(),
            )
        )
        await asyncio.wait_for(caller.started.wait(), timeout=1)
        loaded = await asyncio.wait_for(store.load("inner_state_v1"), timeout=0.1)
        assert loaded["mood"] == inner_state.DEFAULT_STATE["mood"]
        caller.release.set()
        await asyncio.wait_for(task, timeout=1)
        assert store.state["mood"] == "无语"

    asyncio.run(_run())


def test_data_store_mutate_keeps_lock_until_cancelled_worker_finishes(monkeypatch) -> None:  # noqa: ANN001
    data_store = load_personification_module("plugin.personification.core.data_store")
    store = object.__new__(data_store.DataStore)
    store._async_locks = {}
    started = threading.Event()
    release = threading.Event()

    def _mutate_sync(_name: str, mutator):  # noqa: ANN001
        started.set()
        release.wait(timeout=2)
        return mutator({"value": 0})

    monkeypatch.setattr(store, "mutate_sync", _mutate_sync)

    async def _run() -> None:
        first = asyncio.create_task(store.mutate("state", lambda current: {"value": current["value"] + 1}))
        assert await asyncio.to_thread(started.wait, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        second = asyncio.create_task(store.mutate("state", lambda current: {"value": current["value"] + 2}))
        await asyncio.sleep(0.02)
        assert not second.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(second, timeout=1) == {"value": 2}

    asyncio.run(_run())
