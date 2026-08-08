from __future__ import annotations

import asyncio
import json

from ._loader import load_personification_module


registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
handoff_mod = load_personification_module(
    "plugin.personification.agent.runtime.social_video_handoff"
)


def _packet(items) -> str:  # noqa: ANN001
    return json.dumps(
        {
            "schema_version": 1,
            "trust": "untrusted_data_only",
            "items": items,
            "partial": False,
            "platform_statuses": {},
            "filtered_counts": {},
            "warnings": [],
            "retrieved_at": 0,
            "expires_at": 0,
            "packet_id": "packet_test",
        },
        ensure_ascii=False,
    )


def test_social_video_handoff_reads_and_analyzes_every_video() -> None:
    registry = registry_mod.ToolRegistry()
    read_calls: list[str] = []
    vision_calls: list[str] = []

    async def _read(**kwargs):  # noqa: ANN003
        content_id = str(kwargs["content_id"])
        read_calls.append(content_id)
        return _packet(
            [
                {
                    "platform": "bilibili",
                    "content_id": content_id,
                    "content_type": "video",
                    "source_group_id": f"source_{content_id}",
                    "video_ref": f"https://cdn{content_id}.bilivideo.com/video.mp4",
                    "caption_or_body": "视频正文和字幕上下文",
                    "discussion": [{"type": "subtitle", "text": "字幕证据"}],
                }
            ]
        )

    async def _vision(**kwargs):  # noqa: ANN003
        vision_calls.append(kwargs["videos"][0])
        return json.dumps({"scene_summary": kwargs["videos"][0]}, ensure_ascii=False)

    registry.register(
        registry_mod.AgentTool(
            name="social_content_read",
            description="",
            parameters={},
            handler=_read,
            metadata={"remote_name": "social_content_read"},
        )
    )
    registry.register(
        registry_mod.AgentTool(
            name="vision_analyze",
            description="",
            parameters={},
            handler=_vision,
        )
    )
    search = _packet(
        [
            {
                "platform": "bilibili",
                "content_id": "1",
                "content_type": "video",
                "source_group_id": "source_1",
            },
            {
                "platform": "bilibili",
                "content_id": "2",
                "content_type": "video",
                "source_group_id": "source_2",
            },
            {
                "platform": "xiaoheihe",
                "content_id": "3",
                "content_type": "article",
            },
        ]
    )

    result = asyncio.run(
        handoff_mod.run_social_video_handoff(
            registry=registry,
            search_result=search,
            query="分析视频",
        )
    )

    assert result.status == "complete"
    assert read_calls == ["1", "2"]
    assert len(vision_calls) == 2
    assert [item["source_group_id"] for item in result.analyses] == ["source_1", "source_2"]
    assert all(item["trust"] == "untrusted_data_only" for item in result.analyses)
    assert all("media_token" not in item for item in result.analyses)
    assert [item["source_group_id"] for item in result.detail_evidence] == ["source_1", "source_2"]
    assert all(item["trust"] == "untrusted_data_only" for item in result.detail_evidence)


def test_social_video_handoff_rejects_untrusted_video_ref() -> None:
    registry = registry_mod.ToolRegistry()

    async def _read(**_kwargs):  # noqa: ANN003
        return _packet(
            [
                {
                    "platform": "bilibili",
                    "content_id": "1",
                    "content_type": "video",
                    "video_ref": "https://example.com/private.mp4",
                }
            ]
        )

    async def _vision(**_kwargs):  # noqa: ANN003
        raise AssertionError("invalid video_ref must not reach vision")

    registry.register(
        registry_mod.AgentTool(
            name="social_content_read",
            description="",
            parameters={},
            handler=_read,
            metadata={"remote_name": "social_content_read"},
        )
    )
    registry.register(
        registry_mod.AgentTool(
            name="vision_analyze",
            description="",
            parameters={},
            handler=_vision,
        )
    )

    result = asyncio.run(
        handoff_mod.run_social_video_handoff(
            registry=registry,
            search_result=_packet(
                [{"platform": "bilibili", "content_id": "1", "content_type": "video"}]
            ),
            query="分析",
        )
    )

    assert result.status == "partial"
    assert result.analyses == []
    assert result.failures[0]["diagnostic_code"] == "social_video_ref_unavailable"


def test_background_social_video_job_sends_one_synthesized_reply() -> None:
    registry = registry_mod.ToolRegistry()
    sent: list[str] = []

    async def _read(**_kwargs):  # noqa: ANN003
        return _packet(
            [
                {
                    "platform": "bilibili",
                    "content_id": "1",
                    "content_type": "video",
                    "source_group_id": "source_1",
                    "video_ref": "https://cdn.bilivideo.com/video.mp4",
                }
            ]
        )

    async def _vision(**_kwargs):  # noqa: ANN003
        return json.dumps({"scene_summary": "一个游戏梗视频"}, ensure_ascii=False)

    registry.register(
        registry_mod.AgentTool(
            name="social_content_read",
            description="",
            parameters={},
            handler=_read,
            metadata={"remote_name": "social_content_read"},
        )
    )
    registry.register(
        registry_mod.AgentTool(name="vision_analyze", description="", parameters={}, handler=_vision)
    )

    class _Caller:
        async def chat_with_tools(self, *_args, **_kwargs):  # noqa: ANN001
            return type("Response", (), {"content": "这段视频是在用画面里的动作接一个游戏梗。"})()

    class _Executor:
        user_target = "100"
        event = type("Event", (), {"group_id": "200", "user_id": "100"})()

        async def send_text(self, text):  # noqa: ANN001
            sent.append(text)

    async def _scenario() -> bool:
        started = handoff_mod.start_background_social_video_research(
            registry=registry,
            executor=_Executor(),
            tool_caller=_Caller(),
            messages=[{"role": "user", "content": "这个视频什么意思"}],
            search_result=_packet(
                [
                    {
                        "platform": "bilibili",
                        "content_id": "1",
                        "content_type": "video",
                        "source_group_id": "source_1",
                        "canonical_url": "https://www.bilibili.com/video/BV1test",
                    }
                ]
            ),
            query="解释视频里的梗",
            citation_mode="none",
        )
        await asyncio.sleep(0.05)
        return started

    assert asyncio.run(_scenario()) is True
    assert sent == ["这段视频是在用画面里的动作接一个游戏梗。"]


def test_background_social_video_job_does_not_suppress_turn_without_videos() -> None:
    started = handoff_mod.start_background_social_video_research(
        registry=registry_mod.ToolRegistry(),
        executor=type(
            "Executor",
            (),
            {
                "user_target": "100",
                "event": type("Event", (), {"group_id": "200", "user_id": "100"})(),
                "send_text": staticmethod(lambda _text: None),
            },
        )(),
        tool_caller=object(),
        messages=[],
        search_result=_packet(
            [{"platform": "tieba", "content_id": "1", "content_type": "post"}]
        ),
        query="普通帖子",
        citation_mode="none",
    )

    assert started is False
