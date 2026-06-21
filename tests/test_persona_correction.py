from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

persona_service = load_personification_module("plugin.personification.core.persona_service")


def test_parse_structured_filters_unknown() -> None:
    text = "【性别推测】：女\n【职业推测】：信息不足\n【兴趣领域】：绘画、音乐"
    s = persona_service.parse_persona_structured(text)
    assert s["gender"] == "女"
    assert s["interests"] == "绘画、音乐"
    assert "occupation" not in s  # 信息不足被过滤


def _make_store(prev_text: str, captured: list):
    class _Snap:
        profile_text = prev_text
        profile_json = {}
        updated_at = 0

    class _FakeProfileSvc:
        def get_core_profile(self, _uid):
            return _Snap()

    store = persona_service.PersonaStore(
        data_dir="/tmp",
        tool_caller=SimpleNamespace(),
        history_max=10,
        logger=SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None),
        profile_service=_FakeProfileSvc(),
    )
    # 避免真实落库：捕获保存
    def _capture(uid, entry, clear, *, corrections=None):
        captured.append((uid, entry.data, corrections))
    store._save_persona_sync = _capture  # type: ignore[assignment]
    return store


def test_user_correction_prepends_marked_block() -> None:
    captured: list = []
    store = _make_store("【性别推测】：男\n【职业推测】：学生", captured)
    entry = asyncio.run(store.apply_user_correction("u1", {"性别": "女", "职业": "设计师"}))
    assert entry is not None
    assert entry.data.startswith("【用户更正（最高优先级，请始终保留）】")
    assert "性别：女（用户本人确认）" in entry.data
    assert "设计师" in entry.data
    # 原画像保留在更正块之后
    assert "学生" in entry.data
    uid, text, corrections = captured[-1]
    assert corrections == {"性别": "女", "职业": "设计师"}


def test_user_correction_replaces_old_correction_block() -> None:
    captured: list = []
    old = "【用户更正（最高优先级，请始终保留）】\n- 性别：男（用户本人确认）\n\n【职业推测】：老师"
    store = _make_store(old, captured)
    entry = asyncio.run(store.apply_user_correction("u1", {"性别": "女"}))
    # 不应堆叠两个更正块
    assert entry.data.count("【用户更正") == 1
    assert "性别：女" in entry.data
    assert "老师" in entry.data
