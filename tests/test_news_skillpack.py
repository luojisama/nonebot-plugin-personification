from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

news_impl = load_personification_module("plugin.personification.skills.skillpacks.news.scripts.impl")
news_main = load_personification_module("plugin.personification.skills.skillpacks.news.scripts.main")


class _FakeLogger:
    def __init__(self) -> None:
        self.warning_messages: list[str] = []

    def warning(self, message: str) -> None:
        self.warning_messages.append(message)


def _assert_canonical_failure(result: str, error: str) -> None:
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"] == error
    assert result == json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


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


@pytest.mark.parametrize(
    ("platform", "endpoint"),
    [
        ("百度", "/v2/baidu/hot"),
        ("头条", "/v2/toutiao"),
        ("小红书", "/v2/rednote"),
    ],
)
def test_build_trending_tool_supports_core_platforms(monkeypatch, platform, endpoint) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == endpoint
        return [{"title": f"{platform}热点"}]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_trending_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler(platform=platform))

    assert f"【{platform} 热搜 Top10】" in result
    assert f"1. {platform}热点" in result


@pytest.mark.parametrize(
    ("source", "endpoint", "item", "expected"),
    [
        ("IT之家", "/v2/it-news", {"title": "科技头条", "description": "科技摘要"}, "科技摘要"),
        ("Hacker News", "/v2/hacker-news/top", {"title": "Show HN", "score": 321}, "321 points"),
    ],
)
def test_build_tech_news_tool_supports_it_and_hacker_news(
    monkeypatch,
    source,
    endpoint,
    item,
    expected,
) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == endpoint
        return [item]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_tech_news_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler(source=source))

    assert f"【{source} 科技资讯】" in result
    assert item["title"] in result
    assert expected in result


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


def test_build_ai_news_tool_falls_back_to_latest_available_issue(monkeypatch) -> None:  # noqa: ANN001
    seen_params: list[object] = []

    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url
        assert path == "/v2/ai-news"
        seen_params.append(params)
        if params is None:
            return {"date": "2026-04-24", "news": []}
        assert params == {"all": 1}
        return {
            "date": "all",
            "news": [
                {"title": "最近一期", "detail": "最新内容", "date": "2026-04-23"},
                {"title": "更早一期", "detail": "旧内容", "date": "2026-04-22"},
            ],
        }

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_ai_news_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    assert seen_params == [None, {"all": 1}]
    assert "【AI 资讯 2026-04-23】" in result
    assert "最近一期" in result
    assert "更早一期" not in result


@pytest.mark.parametrize("items_key", ["news", "items", "list"])
def test_build_ai_news_tool_returns_no_results_for_empty_items(monkeypatch, items_key) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, _path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        return {"date": "2026-04-23", items_key: []}

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_ai_news_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    _assert_canonical_failure(result, "no_results")
    assert "http://" not in result
    assert "https://" not in result


def test_build_ai_news_tool_returns_no_results_without_valid_title(monkeypatch) -> None:  # noqa: ANN001
    leaked_url = "https://upstream.example/private?token=secret"

    async def _fake_fetch(_remote_base_url, _path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        return {
            "date": "2026-04-23",
            "news": [
                {"title": "", "url": leaked_url},
                {"title": "   ", "summary": "raw upstream detail"},
                {"summary": leaked_url},
                "invalid item",
            ],
        }

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    tool = news_impl.build_ai_news_tool("https://60s.viki.moe", _FakeLogger(), "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    _assert_canonical_failure(result, "no_results")
    assert leaked_url not in result
    assert "raw upstream detail" not in result


@pytest.mark.parametrize("failure_mode", ["fetch", "parse"])
def test_build_ai_news_tool_returns_safe_fetch_failed_for_exceptions(monkeypatch, failure_mode) -> None:  # noqa: ANN001
    leaked_exception = "upstream exploded at https://secret.example/api?token=raw-secret"

    class _InvalidPayload(dict):
        def get(self, key, default=None):  # noqa: ANN001
            del key, default
            raise ValueError(leaked_exception)

    async def _fake_fetch(_remote_base_url, _path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        if failure_mode == "fetch":
            raise RuntimeError(leaked_exception)
        return _InvalidPayload()

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)
    logger = _FakeLogger()

    tool = news_impl.build_ai_news_tool("https://60s.viki.moe", logger, "http://127.0.0.1:4399")
    result = asyncio.run(tool.handler())

    _assert_canonical_failure(result, "fetch_failed")
    assert leaked_exception not in result
    assert all(leaked_exception not in message for message in logger.warning_messages)


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


def test_legacy_news_tools_use_canonical_empty_and_failure_results(monkeypatch) -> None:  # noqa: ANN001
    async def _empty_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/today-in-history"
        return {"items": []}

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _empty_fetch)
    history_tool = news_impl.build_history_today_tool(
        "https://60s.viki.moe",
        _FakeLogger(),
        "http://127.0.0.1:4399",
    )
    _assert_canonical_failure(asyncio.run(history_tool.handler()), "no_results")

    async def _failed_fetch(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("private upstream detail")

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _failed_fetch)
    daily_tool = news_impl.build_daily_news_tool(
        "https://60s.viki.moe",
        _FakeLogger(),
        "http://127.0.0.1:4399",
    )
    _assert_canonical_failure(asyncio.run(daily_tool.handler()), "fetch_failed")


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


def test_main_routes_tech_news_source(monkeypatch) -> None:  # noqa: ANN001
    async def _fake_fetch(_remote_base_url, path, *, local_base_url=None, params=None):  # noqa: ANN001
        del local_base_url, params
        assert path == "/v2/hacker-news/top"
        return [{"title": "Open source release", "score": 42}]

    monkeypatch.setattr(news_impl, "_fetch_v2_data", _fake_fetch)

    result = asyncio.run(news_main.run(topic="tech_news", source="Hacker News"))

    assert "【Hacker News 科技资讯】" in result
    assert "42 points" in result


def test_build_tools_registers_core_news_package() -> None:
    runtime = SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_60s_enabled=True,
            personification_60s_api_base="https://60s.viki.moe",
            personification_60s_local_api_base="http://127.0.0.1:4399",
        ),
        logger=_FakeLogger(),
    )

    names = {tool.name for tool in news_main.build_tools(runtime)}

    assert {"get_ai_news", "get_tech_news", "get_trending"} <= names
