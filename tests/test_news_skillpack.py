from __future__ import annotations

import asyncio
from types import SimpleNamespace

from ._loader import load_personification_module

news_impl = load_personification_module("plugin.personification.skills.skillpacks.news.scripts.impl")
news_main = load_personification_module("plugin.personification.skills.skillpacks.news.scripts.main")


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


def test_build_trending_tool_supports_list_payload_and_bili_endpoint(monkeypatch) -> None:  # noqa: ANN001
    captured: dict[str, object] = {}

    async def _fake_fetch(remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        captured.update(
            {
                "remote_base_url": remote_base_url,
                "path": path,
                "local_base_url": local_base_url,
                "params": params,
            }
        )
        return [{"title": "热点一"}, {"title": "热点二"}]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_trending_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler(platform="B站"))

    assert captured["path"] == "/v2/bili"
    assert "1. 热点一" in result
    assert "2. 热点二" in result


def test_build_ai_news_tool_reads_ai_news_payload(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/ai-news"
        return {
            "date": "2026-04-23",
            "news": [
                {"title": "新模型发布", "summary": "推理成本继续下降", "source": "OpenAI"},
            ],
        }

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_ai_news_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    assert "【AI 资讯 2026-04-23】" in result
    assert "1. 新模型发布 [OpenAI]" in result
    assert "推理成本继续下降" in result


def test_build_joke_tool_reads_duanzi_field(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/duanzi"
        return {"duanzi": "一个人最长的恋爱史，大概就是自恋了。"}

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_joke_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    assert result == "一个人最长的恋爱史，大概就是自恋了。"


def test_build_history_today_tool_reads_items_field(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/today-in-history"
        return {
            "date": "8-26",
            "items": [
                {"year": "1676", "title": "英国首相罗伯特·沃波尔出生"},
                {"year": "1743", "title": "拉瓦锡出生"},
            ],
        }

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_history_today_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    assert "1676年：英国首相罗伯特·沃波尔出生" in result
    assert "1743年：拉瓦锡出生" in result


def test_build_epic_games_tool_supports_list_payload_with_free_end(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/epic"
        return [
            {
                "title": "Project Winter",
                "description": "In Project Winter, survival is just the beginning.",
                "is_free_now": True,
                "free_end": "2025/09/25 23:00:00",
            },
            {
                "title": "Jorel's Brother",
                "is_free_now": False,
                "free_end": "2025/10/02 23:00:00",
            },
        ]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_epic_games_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    assert "《Project Winter》 免费至 09月25日" in result
    assert "In Project Winter, survival is just the beginning." in result
    assert "《Jorel's Brother》 预计免费至 10月02日" in result


def test_main_trending_uses_same_list_compatibility(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/bili"
        return [{"title": "热点一"}]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    result = asyncio.run(news_main._trending("B站"))

    assert "1. 热点一" in result


def test_main_epic_uses_updated_list_payload(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/epic"
        return [
            {
                "title": "Samorost 2",
                "is_free_now": True,
                "free_end": "2025/09/25 23:00:00",
            }
        ]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    result = asyncio.run(news_main._epic())

    assert "《Samorost 2》 免费至 09月25日" in result


def test_main_ai_news_is_available(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/ai-news"
        return {
            "date": "2026-04-23",
            "news": [{"title": "AI 头条", "summary": "一条摘要"}],
        }

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    result = asyncio.run(news_main._ai_news())

    assert "AI 头条" in result
    assert "一条摘要" in result


def test_main_joke_and_history_follow_updated_v2_paths(monkeypatch) -> None:  # noqa: ANN001
    seen_paths: list[str] = []

    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        seen_paths.append(path)
        if path == "/v2/duanzi":
            return {"duanzi": "笑话内容"}
        if path == "/v2/today-in-history":
            return {"items": [{"year": "2008", "title": "一件大事"}]}
        raise AssertionError(path)

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    joke = asyncio.run(news_main._joke())
    history = asyncio.run(news_main._history())

    assert joke == "笑话内容"
    assert "2008年：一件大事" in history
    assert seen_paths == ["/v2/duanzi", "/v2/today-in-history"]
