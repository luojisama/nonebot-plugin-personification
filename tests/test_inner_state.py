from __future__ import annotations

from datetime import datetime, timedelta

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


def test_merge_state_keeps_mood_short_and_does_not_concatenate() -> None:
    current = {
        "mood": "平静",
        "energy": "正常",
        "updated_at": (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
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
