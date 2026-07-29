from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ._loader import load_personification_module

pipeline_sticker = load_personification_module("plugin.personification.handlers.reply_pipeline.pipeline_sticker")
sticker_impl = load_personification_module("plugin.personification.skills.skillpacks.sticker_tool.scripts.impl")
sticker_feedback = load_personification_module("plugin.personification.core.sticker_feedback")


def _make_workspace_temp_dir(prefix: str) -> Path:
    base_dir = Path(__file__).resolve().parent / ".tmp"
    base_dir.mkdir(parents=True, exist_ok=True)
    temp_dir = base_dir / f"{prefix}{uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    return temp_dir


def test_maybe_choose_reply_sticker_respects_semantic_gate() -> None:
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_sticker_probability=1.0,
            personification_sticker_path="",
        ),
        message_segment_cls=SimpleNamespace(image=lambda value: value),
        call_ai_api=None,
        logger=SimpleNamespace(info=lambda *_a, **_k: None),
    )

    result = asyncio.run(
        pipeline_sticker.maybe_choose_reply_sticker(
            runtime=runtime,
            group_id="10001",
            group_config={"sticker_enabled": True},
            semantic_frame=SimpleNamespace(sticker_appropriate=False),
            reply_content="收到",
            raw_message_text="你好",
            message_text="你好",
            message_content="你好",
            image_summary_suffix="",
            is_private_session=False,
            is_random_chat=False,
            is_group_idle_active=False,
            force_mode=None,
            strip_injected_visual_summary=lambda text: text,
        )
    )

    assert result == (None, "")


def test_rank_sticker_candidates_uses_feedback_score(monkeypatch) -> None:  # noqa: ANN001
    temp_dir = _make_workspace_temp_dir("stickers-")
    try:
        a_path = temp_dir / "a.png"
        b_path = temp_dir / "b.png"
        a_path.write_bytes(b"a")
        b_path.write_bytes(b"b")

        monkeypatch.setattr(sticker_impl, "list_local_sticker_files", lambda *_a, **_k: [a_path, b_path])
        monkeypatch.setattr(
            sticker_impl,
            "load_sticker_metadata",
            lambda *_a, **_k: {
                "a.png": {"mood_tags": ["搞笑"], "scene_tags": ["接梗"], "description": "普通接梗"},
                "b.png": {"mood_tags": ["搞笑"], "scene_tags": ["接梗"], "description": "更强接梗"},
            },
        )

        candidates = sticker_impl.rank_sticker_candidates(
            temp_dir,
            mood="搞笑|接梗",
            context="现在适合接梗",
            proactive=False,
            plugin_config=SimpleNamespace(personification_sticker_semantic=True),
            feedback_state={
                "items": {
                    "a": {
                        "sent_count": 4,
                        "positive_count": 1,
                    },
                    "b": {
                        "sent_count": 4,
                        "positive_count": 4,
                    }
                }
            },
        )

        assert candidates[0]["path"].stem == "b"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_select_sticker_can_filter_gif_media_type() -> None:
    temp_dir = _make_workspace_temp_dir("stickers-gif-")
    try:
        (temp_dir / "static.png").write_bytes(b"png")
        (temp_dir / "animated.gif").write_bytes(b"gif")

        selected = sticker_impl.select_sticker(
            temp_dir,
            mood="淡定|表达疑惑",
            context="随手发个动图",
            proactive=False,
            plugin_config=SimpleNamespace(personification_sticker_semantic=True),
            media_type="gif",
        )

        assert selected.endswith("animated.gif")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_send_sticker_tool_queues_gif_action() -> None:
    temp_dir = _make_workspace_temp_dir("stickers-send-gif-")
    try:
        (temp_dir / "animated.gif").write_bytes(b"gif")
        queued: list[dict] = []

        class _Executor:
            def queue_action(self, action, params):  # noqa: ANN001
                queued.append({"type": action, "params": params})

        tool = sticker_impl.build_send_sticker_tool(
            temp_dir,
            SimpleNamespace(personification_sticker_semantic=True),
            _Executor(),
        )

        result = asyncio.run(
            tool.handler(
                mood="淡定|表达疑惑",
                context="用户想看一个动图表情",
                media_type="gif",
            )
        )

        assert '"queued": true' in result
        assert queued[0]["type"] == "send_sticker"
        assert queued[0]["params"]["path"].endswith("animated.gif")
        assert queued[0]["params"]["history_text"] == "[GIF表情包:animated]"
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


class _FakeStore:
    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}

    async def load(self, name: str):  # noqa: ANN001
        return self.payloads.get(name, {})

    async def mutate(self, name: str, mutator):  # noqa: ANN001
        current = self.payloads.get(name, {})
        updated = mutator(current)
        self.payloads[name] = updated
        return updated


class _PositiveCaller:
    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        del messages, tools, use_builtin_search
        return SimpleNamespace(content="positive")


def test_review_pending_sticker_reaction_records_positive(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(sticker_feedback, "get_data_store", lambda: store)

    async def _run() -> dict:
        await sticker_feedback.record_sticker_sent("cheer")
        sticker_feedback.mark_pending_sticker_reaction("group:10001", "cheer")
        reviewed = await sticker_feedback.review_pending_sticker_reaction(
            "group:10001",
            "哈哈这个图还挺对味",
            tool_caller=_PositiveCaller(),
            logger=SimpleNamespace(debug=lambda *_a, **_k: None),
        )
        assert reviewed is True
        return await sticker_feedback.load_sticker_feedback()

    state = asyncio.run(_run())
    assert state["items"]["cheer"]["sent_count"] == 1
    assert state["items"]["cheer"]["positive_count"] == 1
