from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from ._loader import load_personification_module


def test_social_research_server_exposes_only_read_tools_and_control_is_not_listed(tmp_path: Path) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    project_root = Path(__file__).resolve().parents[2]

    async def run():
        async with compat.McpStdioClient(
            command=sys.executable,
            args=[
                str(
                    Path(__file__).resolve().parents[1]
                    / "nonebot_plugin_personification"
                    / "native_mcp"
                    / "social_research"
                    / "entrypoint.py"
                )
            ],
            env={**os.environ, "PERSONIFICATION_SOCIAL_DATA_DIR": str(tmp_path / "social")},
            cwd=str(project_root),
            timeout=8,
        ) as client:
            tools = await client.list_tools()
            status = await client.request("personification/builtin/status", {})
            return client.protocol_version, tools, status

    version, tools, status = asyncio.run(run())
    assert version == "2025-11-25"
    assert {tool["name"] for tool in tools} == {
        "social_content_search",
        "social_content_read",
        "research_game_slang",
    }
    assert all(tool["annotations"]["readOnlyHint"] is True for tool in tools)
    assert not any("auth" in tool["name"] or "configure" in tool["name"] for tool in tools)
    assert set(status["platforms"]) == {"bilibili", "douyin", "tieba", "xiaoheihe"}
    assert all(item["state"] == "disabled" for item in status["platforms"].values())
    assert all("enabled" not in item["config"] for item in status["platforms"].values())

    search = next(tool for tool in tools if tool["name"] == "social_content_search")
    research = next(tool for tool in tools if tool["name"] == "research_game_slang")
    assert search["inputSchema"]["properties"]["limit"]["default"] == 10
    assert research["inputSchema"]["properties"]["limit"]["default"] == 10
    assert {"aggregation", "source_groups"} <= set(search["outputSchema"]["properties"])


def test_platform_status_keeps_auth_probe_failure_partial_and_config_control_fields_separate(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FailingAdapter:
        async def authenticated(self):
            raise OSError("private browser detail")

        def capabilities(self):
            return {"search": True}

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        service._config["xiaoheihe"]["enabled"] = True
        service.adapters["xiaoheihe"] = FailingAdapter()
        try:
            return await service.status()
        finally:
            await service.close()

    status = asyncio.run(run())
    platform = status["platforms"]["xiaoheihe"]
    assert platform["enabled"] is True
    assert platform["state"] == "unavailable"
    assert platform["error_code"] == "platform_request_failed"
    assert "enabled" not in platform["config"]
    assert "private browser detail" not in json.dumps(platform, ensure_ascii=False)


def test_balanced_filter_requires_both_video_thresholds_and_filters_marketing() -> None:
    models = load_personification_module("plugin.personification.native_mcp.social_research.models")
    config = dict(models.DEFAULT_PLATFORM_CONFIG)
    high_comments = models.apply_quality_filter(
        {
            "content_type": "video",
            "title": "小众攻略",
            "caption_or_body": "正常讨论",
            "stats": {"play_count": 100, "comment_count": 20},
        },
        config,
    )
    assert high_comments["retained"] is True

    double_low = models.apply_quality_filter(
        {
            "content_type": "video",
            "title": "小众攻略",
            "caption_or_body": "正常讨论",
            "stats": {"play_count": 100, "comment_count": 1},
        },
        config,
    )
    assert double_low["retained"] is False
    assert double_low["filtered_reason"] == "low_video_engagement"

    marketing = models.apply_quality_filter(
        {
            "content_type": "video",
            "title": "低价代练优惠下单",
            "caption_or_body": "加微信 abcdef 私信购买 https://example.test",
            "commercial_label": True,
            "stats": {"play_count": 100000, "comment_count": 100},
        },
        config,
    )
    assert marketing["retained"] is False
    assert marketing["filtered_reason"] == "marketing_risk"
    assert marketing["marketing_reasons"]


def test_social_search_returns_partial_packet_without_leaking_adapter_error(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        async def authenticated(self):
            return True

        async def search(self, query, *, limit, timeout_seconds):
            assert query == "刘涛"
            return [
                {
                    "platform": "bilibili",
                    "content_type": "video",
                    "content_id": "BV1",
                    "canonical_url": "https://www.bilibili.com/video/BV1",
                    "title": "刘涛是什么意思",
                    "caption_or_body": "玩家讨论",
                    "cover_ref": "https://i0.hdslb.com/test.jpg",
                    "author": {"display_name": "", "fingerprint": ""},
                    "published_at": 0,
                    "stats": {"play_count": 10000, "comment_count": 20},
                    "discussion": [],
                    "content_fingerprint": "x",
                }
            ]

    class FailingAdapter:
        async def authenticated(self):
            raise RuntimeError("private-cookie-value")

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        service._config["bilibili"]["enabled"] = True
        service._config["douyin"]["enabled"] = True
        service.adapters["bilibili"] = FakeAdapter()
        service.adapters["douyin"] = FailingAdapter()
        try:
            return await service.search(
                {"query": "刘涛", "platforms": ["bilibili", "douyin"], "limit": 5, "quality_mode": "balanced"}
            )
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert packet["trust"] == "untrusted_data_only"
    assert packet["partial"] is True
    assert packet["items"][0]["content_id"] == "BV1"
    serialized = json.dumps(packet, ensure_ascii=False)
    assert "private-cookie-value" not in serialized
    assert packet["platform_statuses"]["douyin"]["error_code"] == "platform_request_failed"


def test_social_search_honors_content_type_filter(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        async def authenticated(self):
            return True

        async def search(self, _query, *, limit, timeout_seconds):
            return [{
                "platform": "bilibili",
                "content_type": "video",
                "content_id": "BV1",
                "canonical_url": "https://www.bilibili.com/video/BV1",
                "title": "视频",
                "caption_or_body": "内容",
                "cover_ref": "",
                "author": {"display_name": "", "fingerprint": ""},
                "published_at": 0,
                "stats": {"play_count": 10000, "comment_count": 20},
                "discussion": [],
                "content_fingerprint": "x",
            }]

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        service._config["bilibili"]["enabled"] = True
        service.adapters["bilibili"] = FakeAdapter()
        try:
            return await service.search({"query": "测试", "platforms": ["bilibili"], "content_types": ["article"]})
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert packet["items"] == []
    assert packet["filtered_counts"]["bilibili"] == 1


def _fake_social_item(platform: str, index: int, *, fingerprint: str = "") -> dict:
    is_video = platform in {"bilibili", "douyin"}
    content_id = f"BV{index}" if platform == "bilibili" else str(100000 + index)
    host = {
        "bilibili": "www.bilibili.com/video/",
        "douyin": "www.douyin.com/video/",
        "tieba": "tieba.baidu.com/p/",
        "xiaoheihe": "xiaoheihe.cn/app/bbs/link/",
    }[platform]
    return {
        "platform": platform,
        "content_type": "video" if is_video else "article" if platform == "xiaoheihe" else "post",
        "content_id": content_id,
        "canonical_url": f"https://{host}{content_id}",
        "title": f"{platform} 资料 {index}",
        "caption_or_body": f"{platform} 的独立讨论内容 {index}",
        "cover_ref": "",
        "author": {"display_name": "", "fingerprint": ""},
        "published_at": index,
        "stats": (
            {"play_count": 200000 - index, "comment_count": 100}
            if is_video
            else {"reply_count": 20, "like_count": 50}
        ),
        "discussion": [],
        "content_fingerprint": fingerprint or f"{platform}-{index}",
    }


def test_social_search_default_is_global_ten_with_fair_platform_coverage(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        def __init__(self, platform: str) -> None:
            self.platform = platform
            self.limits: list[int] = []

        async def authenticated(self):  # noqa: ANN201
            return True

        async def search(self, _query, *, limit, timeout_seconds):  # noqa: ANN001, ANN201
            self.limits.append(limit)
            return [_fake_social_item(self.platform, index) for index in range(limit)]

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        service._config["bilibili"]["enabled"] = True
        service._config["xiaoheihe"]["enabled"] = True
        bilibili = FakeAdapter("bilibili")
        xiaoheihe = FakeAdapter("xiaoheihe")
        service.adapters["bilibili"] = bilibili
        service.adapters["xiaoheihe"] = xiaoheihe
        try:
            packet = await service.search({"query": "花来"})
            return packet, bilibili.limits, xiaoheihe.limits
        finally:
            await service.close()

    packet, bilibili_limits, xiaoheihe_limits = asyncio.run(run())
    assert len(packet["items"]) == 10
    assert bilibili_limits == [10]
    assert xiaoheihe_limits == [10]
    assert packet["aggregation"]["requested_limit"] == 10
    assert packet["aggregation"]["returned_count"] == 10
    assert packet["aggregation"]["selected_platforms"] == ["bilibili", "xiaoheihe"]
    assert packet["aggregation"]["covered_platforms"] == ["bilibili", "xiaoheihe"]
    assert packet["aggregation"]["satisfies_request"] is True
    assert {item["platform"] for item in packet["items"]} == {"bilibili", "xiaoheihe"}
    assert all(item["source_group_id"].startswith("source_") for item in packet["items"])


def test_social_search_groups_reposts_and_prefers_unseen_sources(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        def __init__(self, items: list[dict]) -> None:
            self.items = items

        async def authenticated(self):  # noqa: ANN201
            return True

        async def search(self, _query, *, limit, timeout_seconds):  # noqa: ANN001, ANN201
            return self.items[:limit]

    shared = "same-reposted-content"

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        for platform in ("bilibili", "xiaoheihe"):
            service._config[platform]["enabled"] = True
        service.adapters["bilibili"] = FakeAdapter([
            _fake_social_item("bilibili", 1, fingerprint=shared),
            _fake_social_item("bilibili", 2),
        ])
        service.adapters["xiaoheihe"] = FakeAdapter([
            _fake_social_item("xiaoheihe", 1, fingerprint=shared),
            _fake_social_item("xiaoheihe", 2),
        ])
        try:
            return await service.search({"query": "花来", "limit": 3})
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert len(packet["items"]) == 3
    assert packet["aggregation"]["source_group_count"] == 3
    assert len({item["source_group_id"] for item in packet["items"]}) == 3
    assert all(group["member_count"] == 1 for group in packet["source_groups"])


def test_social_search_without_enabled_platform_fails_with_stable_code(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        try:
            return await service.search({"query": "花来"})
        finally:
            await service.close()

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        assert str(exc) == "no_enabled_platform"
        assert service_mod._safe_operation_code(exc) == "no_enabled_platform"
    else:
        raise AssertionError("missing enabled platforms must fail closed")


def test_research_game_slang_keeps_search_card_when_detail_read_fails(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        first = {**_fake_social_item("bilibili", 1), "source_group_id": "source_one"}
        second = {**_fake_social_item("xiaoheihe", 2), "source_group_id": "source_two"}

        async def fake_search(params):  # noqa: ANN001
            assert params["limit"] == 2
            assert "platforms" not in params
            return {
                "items": [first, second],
                "platform_statuses": {"bilibili": {"state": "ready"}, "xiaoheihe": {"state": "ready"}},
                "partial": False,
                "warnings": [],
                "filtered_counts": {},
                "aggregation": {
                    "requested_limit": 2,
                    "returned_count": 2,
                    "source_group_count": 2,
                    "coverage_status": "complete",
                    "satisfies_request": True,
                },
            }

        async def fake_read(params):  # noqa: ANN001
            if params["platform"] == "xiaoheihe":
                raise RuntimeError("detail_content_unavailable")
            return {"items": [{**first, "caption_or_body": "详情正文"}]}

        service.search = fake_search
        service.read = fake_read
        try:
            return await service.research({"term": "花来", "context": "群聊", "limit": 2})
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert len(packet["items"]) == 2
    assert packet["items"][0]["detail_status"] == "ready"
    assert packet["items"][1]["detail_status"] == "detail_content_unavailable"
    assert packet["items"][1]["canonical_url"].startswith("https://xiaoheihe.cn/app/bbs/link/")
    assert packet["partial"] is True
    assert packet["aggregation"]["coverage_status"] == "degraded"
    assert packet["aggregation"]["stages"]["search"]["returned_count"] == 2
    assert packet["aggregation"]["stages"]["detail"]["ready_count"] == 1
    assert packet["aggregation"]["stages"]["detail"]["unavailable_count"] == 1
    assert packet["items"][1]["detail_error_code"] == "detail_content_unavailable"
    assert packet["items"][1]["detail_elapsed_ms"] >= 0


def test_platform_url_validation_rejects_cross_origin_and_credentials(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["bilibili"], browser_mod.BrowserPool(tmp_path))
    assert adapter.validate_url("https://www.bilibili.com/video/BV1/").startswith("https://www.bilibili.com/")
    for value in (
        "https://example.com/video/BV1",
        "http://www.bilibili.com/video/BV1",
        "https://user:pass@www.bilibili.com/video/BV1",
    ):
        try:
            adapter.validate_url(value)
        except ValueError:
            pass
        else:
            raise AssertionError(value)


def test_live_page_state_is_mapped_to_safe_control_code(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    assert service_mod._safe_operation_code(RuntimeError("manual_verification_required")) == "manual_verification_required"
    assert service_mod._safe_operation_code(RuntimeError("risk_controlled")) == "risk_controlled"
    assert service_mod._safe_operation_code(asyncio.TimeoutError()) == "platform_timeout"


def test_xiaoheihe_uses_current_web_search_route() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    spec = adapters_mod.SPECS["xiaoheihe"]
    assert spec.login_url == "https://xiaoheihe.cn/app/bbs/home"
    assert spec.search_url.startswith("https://xiaoheihe.cn/app/search/list?q=")
    assert spec.content_link_selector == 'a[href*="/app/bbs/link/"]'


def test_platform_content_id_parsers_accept_only_real_content_routes(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    pool = browser_mod.BrowserPool(tmp_path)
    cases = {
        "bilibili": ("https://www.bilibili.com/video/BV1abc123/", "BV1abc123"),
        "douyin": ("https://www.douyin.com/video/7520000000000000000", "7520000000000000000"),
        "tieba": ("https://tieba.baidu.com/p/9876543210", "9876543210"),
        "xiaoheihe": ("https://xiaoheihe.cn/app/bbs/link/179364001", "179364001"),
    }
    for platform, (url, expected) in cases.items():
        adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS[platform], pool)
        assert adapter.content_id(url) == expected

    xiaoheihe = adapters_mod.PlatformAdapter(adapters_mod.SPECS["xiaoheihe"], pool)
    for url in (
        "https://xiaoheihe.cn/app/bbs/home",
        "https://xiaoheihe.cn/app/search/list?q=test",
        "https://xiaoheihe.cn/app/user/profile/75746007",
        "https://xiaoheihe.cn/app/bbs/topic/123",
    ):
        assert xiaoheihe.content_id(url) == ""


def test_xiaoheihe_normalizes_www_content_urls_and_builds_canonical_detail_url(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["xiaoheihe"], browser_mod.BrowserPool(tmp_path))

    assert adapter.validate_url("https://www.xiaoheihe.cn/app/bbs/link/179364001") == (
        "https://xiaoheihe.cn/app/bbs/link/179364001"
    )
    assert adapter.url_for_id("179364001") == "https://xiaoheihe.cn/app/bbs/link/179364001"


def test_xiaoheihe_detail_read_waits_for_and_extracts_article_container() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        async def all_inner_texts(self):  # noqa: ANN201
            return []

    class FakePage:
        def __init__(self) -> None:
            self.waited_selector = ""
            self.evaluate_calls = 0

        async def goto(self, url, **_kwargs):  # noqa: ANN001, ANN201
            assert url == "https://xiaoheihe.cn/app/bbs/link/179364001"
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        async def evaluate(self, script):  # noqa: ANN001, ANN201
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {"title": "小黑盒", "body": "公开帖子"}
            assert ".post__content .hb-article" in script
            return {
                "title": "花来成就要注意",
                "description": "S9赛季正文，不含导航与评论",
                "cover": "https://cdn.xiaoheihe.cn/post/cover.jpg",
                "images": [
                    {"url": "https://cdn.xiaoheihe.cn/post/cover.jpg", "alt": "配装图"},
                    {"url": "https://cdn.xiaoheihe.cn/post/result.jpg", "alt": "结算图"},
                ],
                "body": "",
            }

        def locator(self, _selector):  # noqa: ANN001, ANN201
            return FakeLocator()

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["xiaoheihe"], browsers)
    item = asyncio.run(
        adapter.read(
            content_id="",
            url="https://www.xiaoheihe.cn/app/bbs/link/179364001",
            include=["caption"],
            comment_limit=0,
            danmaku_limit=0,
            timeout_seconds=5,
        )
    )

    assert browsers.fake_page.waited_selector == ".hb-bbs-post .post__content .hb-article"
    assert item["content_id"] == "179364001"
    assert item["caption_or_body"] == "S9赛季正文，不含导航与评论"
    assert item["image_urls"] == [
        "https://cdn.xiaoheihe.cn/post/cover.jpg",
        "https://cdn.xiaoheihe.cn/post/result.jpg",
    ]
    assert item["image_count"] == 2


def test_xiaoheihe_search_waits_for_dynamic_results_and_closes_fresh_page() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        async def evaluate_all(self, _script, _options):  # noqa: ANN001, ANN201
            return [
                {
                    "href": "https://xiaoheihe.cn/app/bbs/link/179364001",
                    "title": "花来成就要注意",
                    "text": "花来成就要注意 三角洲行动",
                    "metricText": ["161", "408"],
                    "cover": "https://cdn.xiaoheihe.cn/post/search-cover.jpg",
                    "images": [
                        {"url": "https://cdn.xiaoheihe.cn/post/search-cover.jpg", "alt": "搜索缩略图"},
                        {"url": "https://cdn.xiaoheihe.cn/post/search-extra.jpg", "alt": "第二张"},
                    ],
                }
            ]

    class FakePage:
        def __init__(self) -> None:
            self.waited_selector = ""
            self.closed = False

        async def goto(self, _url, **_kwargs):  # noqa: ANN001, ANN201
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        async def evaluate(self, _script):  # noqa: ANN001, ANN201
            return {"title": "小黑盒", "body": "搜索结果"}

        def locator(self, _selector):  # noqa: ANN001, ANN201
            return FakeLocator()

        async def close(self) -> None:
            self.closed = True

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def fresh_page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["xiaoheihe"], browsers)
    items = asyncio.run(adapter.search("三角洲行动 花来", limit=10, timeout_seconds=5))

    assert browsers.fake_page.waited_selector == 'a[href*="/app/bbs/link/"]'
    assert browsers.fake_page.closed is True
    assert [item["content_id"] for item in items] == ["179364001"]
    assert items[0]["image_urls"] == [
        "https://cdn.xiaoheihe.cn/post/search-cover.jpg",
        "https://cdn.xiaoheihe.cn/post/search-extra.jpg",
    ]
    assert items[0]["image_count"] == 2


def test_detail_read_keeps_selected_source_without_engagement_and_uses_opaque_image_refs(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        async def authenticated(self):  # noqa: ANN201
            return True

        async def read(self, **_kwargs):  # noqa: ANN003, ANN201
            return {
                **_fake_social_item("xiaoheihe", 1),
                "caption_or_body": "作者发布的完整图文正文",
                "cover_ref": "https://cdn.xiaoheihe.cn/post/cover.jpg",
                "image_urls": [
                    "https://cdn.xiaoheihe.cn/post/cover.jpg",
                    "https://cdn.xiaoheihe.cn/post/result.jpg",
                ],
                "image_count": 2,
                "stats": {"reply_count": 0, "like_count": 0},
            }

    async def run():  # noqa: ANN202
        service = service_mod.SocialResearchService(tmp_path)
        service._config["xiaoheihe"]["enabled"] = True
        service.adapters["xiaoheihe"] = FakeAdapter()
        try:
            return await service.read(
                {
                    "platform": "xiaoheihe",
                    "url": "https://xiaoheihe.cn/app/bbs/link/100001",
                    "include": ["caption"],
                }
            )
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert len(packet["items"]) == 1
    item = packet["items"][0]
    assert item["retained"] is False
    assert item["caption_or_body"] == "作者发布的完整图文正文"
    assert "image_urls" not in item
    assert len(item["image_refs"]) == 2
    assert item["cover_ref"] == item["image_refs"][0]
    assert all(ref.startswith("cover_") and len(ref) == 46 for ref in item["image_refs"])


def test_social_search_enriches_selected_xiaoheihe_card_with_author_text_and_images(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    class FakeAdapter:
        async def authenticated(self):  # noqa: ANN201
            return True

        async def search(self, _query, *, limit, timeout_seconds):  # noqa: ANN001, ANN201
            return [
                {
                    **_fake_social_item("xiaoheihe", 1),
                    "caption_or_body": "搜索卡片摘要",
                    "cover_ref": "https://cdn.xiaoheihe.cn/post/search.jpg",
                }
            ][:limit]

        async def read(self, **_kwargs):  # noqa: ANN003, ANN201
            return {
                **_fake_social_item("xiaoheihe", 1),
                "title": "作者的完整图文帖",
                "caption_or_body": "作者正文：花来挑战的完整说明",
                "cover_ref": "https://cdn.xiaoheihe.cn/post/full-1.jpg",
                "image_urls": [
                    "https://cdn.xiaoheihe.cn/post/full-1.jpg",
                    "https://cdn.xiaoheihe.cn/post/full-2.jpg",
                ],
                "image_count": 2,
                "stats": {"reply_count": 0, "like_count": 0},
            }

    async def run():  # noqa: ANN202
        service = service_mod.SocialResearchService(tmp_path)
        service._config["xiaoheihe"]["enabled"] = True
        service.adapters["xiaoheihe"] = FakeAdapter()
        try:
            return await service.search({"query": "花来", "platforms": ["xiaoheihe"], "limit": 1})
        finally:
            await service.close()

    packet = asyncio.run(run())
    item = packet["items"][0]
    assert item["detail_status"] == "ready"
    assert item["title"] == "作者的完整图文帖"
    assert item["caption_or_body"] == "作者正文：花来挑战的完整说明"
    assert item["image_count"] == 2
    assert len(item["image_refs"]) == 2
    assert packet["aggregation"]["xiaoheihe_detail_elapsed_ms"] >= 0


def test_parallel_detail_reads_use_distinct_fresh_pages_and_close_them() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakePage:
        def __init__(self) -> None:
            self.evaluate_calls = 0
            self.closed = False

        async def goto(self, _url, **_kwargs):  # noqa: ANN001, ANN201
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            await asyncio.sleep(0)

        async def evaluate(self, _script):  # noqa: ANN001, ANN201
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {"title": "B站", "body": "视频详情"}
            return {"title": "花来", "description": "夺舍成功", "cover": "", "body": ""}

        async def close(self) -> None:
            self.closed = True

    class FakeBrowsers:
        def __init__(self) -> None:
            self.pages = []

        async def fresh_page(self, _platform):  # noqa: ANN001, ANN201
            page = FakePage()
            self.pages.append(page)
            return page

    async def run():  # noqa: ANN202
        browsers = FakeBrowsers()
        adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["bilibili"], browsers)
        results = await asyncio.gather(
            adapter.read(
                content_id="",
                url="https://www.bilibili.com/video/BV1abc123/",
                include=["caption"],
                comment_limit=0,
                danmaku_limit=0,
                timeout_seconds=5,
            ),
            adapter.read(
                content_id="",
                url="https://www.bilibili.com/video/BV1def456/",
                include=["caption"],
                comment_limit=0,
                danmaku_limit=0,
                timeout_seconds=5,
            ),
        )
        return browsers, results

    browsers, results = asyncio.run(run())
    assert len(browsers.pages) == 2
    assert all(page.closed for page in browsers.pages)
    assert {item["content_id"] for item in results} == {"BV1abc123", "BV1def456"}


def test_platform_login_selectors_cover_current_official_qr_surfaces() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    assert 'img[alt="Scan me!"]' in adapters_mod.SPECS["bilibili"].qr_selectors
    assert '[title*="scan-web"] img' in adapters_mod.SPECS["bilibili"].qr_selectors
    assert 'img[alt="二维码"]' in adapters_mod.SPECS["douyin"].qr_selectors
    assert 'div:text-is("登录")' in adapters_mod.SPECS["douyin"].login_trigger_selectors
    assert adapters_mod.SPECS["douyin"].auth_cookie_names == frozenset({"sessionid", "sessionid_ss"})
    assert "passport_csrf_token" not in adapters_mod.SPECS["douyin"].auth_cookie_names
    assert 'canvas.website-login__qr-canvas' in adapters_mod.SPECS["xiaoheihe"].qr_selectors


def test_cover_registry_returns_opaque_reference_and_enforces_platform_hosts(tmp_path: Path) -> None:
    covers_mod = load_personification_module("plugin.personification.native_mcp.social_research.covers")
    registry = covers_mod.CoverRegistry(tmp_path)
    ref = registry.register("bilibili", "https://i0.hdslb.com/bfs/archive/demo.jpg?signed=internal")
    assert ref.startswith("cover_")
    assert "hdslb" not in ref and "signed" not in ref
    resolved = registry.resolve(ref)
    assert resolved["platform"] == "bilibili"
    assert resolved["url"].startswith("https://i0.hdslb.com/")
    assert registry.register("bilibili", "https://example.com/private.jpg") == ""


def test_auth_session_is_bound_to_owner_and_public_shape_excludes_owner(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(session_id="session", platform="bilibili", owner="admin:device:bilibili")
    pool._auth[session.session_id] = session
    public = pool.public_auth(session)
    assert "owner" not in public
    assert "qr_selectors" not in public
    assert "login_trigger_selectors" not in public
    assert public["qr_revision"] == 0
    assert public["verification_kind"] == ""
    assert public["login_mode"] == "embedded_qr"
    assert 890 <= public["remaining_seconds"] <= 900
    assert "native_process" not in public
    assert "login_url" not in public
    assert "protocol_secret" not in public
    try:
        pool.get_auth("session", "another-device")
    except KeyError:
        pass
    else:
        raise AssertionError("auth session was not bound to owner")


def test_bilibili_protocol_validates_official_challenge_and_poll_states() -> None:
    protocol = load_personification_module("plugin.personification.native_mcp.social_research.bilibili_auth")

    class FakeResponse:
        status = 200

        def __init__(self, payload) -> None:  # noqa: ANN001
            self.payload = payload

        async def json(self):  # noqa: ANN201
            return self.payload

    class FakeRequest:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []
            self.responses = [
                FakeResponse({
                    "code": 0,
                    "data": {
                        "qrcode_key": "a" * 32,
                        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + "a" * 32,
                    },
                }),
                FakeResponse({"code": 0, "data": {"code": 86101, "message": "未扫码"}}),
            ]

        async def get(self, url, **kwargs):  # noqa: ANN001, ANN201
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    async def run():
        request = FakeRequest()
        challenge = await protocol.generate_challenge(request)
        state = await protocol.poll_challenge(request, challenge.key)
        return request, challenge, state

    request, challenge, state = asyncio.run(run())
    assert challenge.key == "a" * 32
    assert challenge.qr_url.startswith("https://account.bilibili.com/h5/account-h5/auth/scan-web?")
    assert state == 86101
    assert request.calls[0][0] == "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    assert request.calls[1][0] == "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    assert request.calls[1][1]["params"] == {"qrcode_key": "a" * 32, "source": "main_web"}
    assert all(call[1]["max_redirects"] == 0 for call in request.calls)


def test_bilibili_protocol_rejects_cross_origin_qr_and_unknown_poll_state() -> None:
    protocol = load_personification_module("plugin.personification.native_mcp.social_research.bilibili_auth")

    class FakeResponse:
        status = 200

        def __init__(self, payload) -> None:  # noqa: ANN001
            self.payload = payload

        async def json(self):  # noqa: ANN201
            return self.payload

    class FakeRequest:
        def __init__(self, response) -> None:  # noqa: ANN001
            self.response = response

        async def get(self, *_args, **_kwargs):  # noqa: ANN001, ANN201
            return self.response

    bad_origin = FakeRequest(FakeResponse({
        "code": 0,
        "data": {"qrcode_key": "b" * 32, "url": "https://example.com/scan?qrcode_key=" + "b" * 32},
    }))
    unknown = FakeRequest(FakeResponse({"code": 0, "data": {"code": 12345, "message": "unexpected"}}))

    async def run() -> list[str]:
        errors: list[str] = []
        try:
            await protocol.generate_challenge(bad_origin)
        except RuntimeError as exc:
            errors.append(str(exc))
        try:
            await protocol.poll_challenge(unknown, "c" * 32)
        except RuntimeError as exc:
            errors.append(str(exc))
        return errors

    assert asyncio.run(run()) == ["bilibili_qr_generate_failed", "bilibili_qr_unknown_state"]


def test_bilibili_protocol_renders_qr_locally_as_png() -> None:
    protocol = load_personification_module("plugin.personification.native_mcp.social_research.bilibili_auth")
    image = protocol.render_qr_png(
        "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + "f" * 32
    )
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(image) < 1024 * 1024


def test_qr_visual_gate_rejects_loading_placeholder_and_accepts_real_qr() -> None:
    from io import BytesIO

    from PIL import Image, ImageDraw

    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    protocol = load_personification_module("plugin.personification.native_mcp.social_research.bilibili_auth")
    placeholder = Image.new("RGB", (156, 156), "#535353")
    ImageDraw.Draw(placeholder).text((30, 70), "QR loading", fill="white")
    output = BytesIO()
    placeholder.save(output, format="PNG")
    real_qr = protocol.render_qr_png(
        "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + "1" * 32
    )

    assert browser_mod.BrowserPool._looks_like_qr_png(output.getvalue()) is False
    assert browser_mod.BrowserPool._looks_like_qr_png(real_qr) is True


def test_bilibili_adapter_routes_embedded_qr_to_protocol_login() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeBrowsers:
        async def start_bilibili_qr_auth(self, owner):  # noqa: ANN001, ANN201
            return {"owner_seen": owner, "login_mode": "protocol_qr"}

        async def start_auth(self, *_args, **_kwargs):  # noqa: ANN001, ANN201
            raise AssertionError("Bilibili must not use the page QR flow")

    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["bilibili"], FakeBrowsers())
    result = asyncio.run(adapter.start_auth("admin:device:bilibili", mode="embedded_qr"))
    assert result == {"owner_seen": "admin:device:bilibili", "login_mode": "protocol_qr"}


def test_xiaoheihe_adapter_keeps_official_qr_page_headless() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeBrowsers:
        async def start_auth(self, platform, owner, login_url, qr_selectors, login_triggers, **kwargs):  # noqa: ANN001, ANN201
            return {
                "platform": platform,
                "owner": owner,
                "login_url": login_url,
                "qr_selectors": qr_selectors,
                "login_triggers": login_triggers,
                **kwargs,
            }

    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["xiaoheihe"], FakeBrowsers())
    result = asyncio.run(adapter.start_auth("admin:device:xiaoheihe", mode="embedded_qr"))
    assert result["platform"] == "xiaoheihe"
    assert result["prefer_headless"] is True


def test_bilibili_qr_login_uses_no_visible_page_and_hides_transaction_key(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    challenge_type = load_personification_module(
        "plugin.personification.native_mcp.social_research.bilibili_auth"
    ).BilibiliQrChallenge
    pool = browser_mod.BrowserPool(tmp_path)

    class FakeContext:
        request = object()

    async def fake_context(platform, *, headless=True):  # noqa: ANN001
        assert platform == "bilibili"
        assert headless is True
        return FakeContext()

    async def fake_generate(_request):  # noqa: ANN001
        return challenge_type(key="secret-transaction-key-12345678", qr_url="https://account.bilibili.com/scan")

    async def page_must_not_open(*_args, **_kwargs):  # noqa: ANN001
        raise AssertionError("protocol login must not open a page")

    monkeypatch.setattr(pool, "context", fake_context)
    monkeypatch.setattr(pool, "page", page_must_not_open)
    monkeypatch.setattr(browser_mod, "generate_challenge", fake_generate)
    monkeypatch.setattr(browser_mod, "render_qr_png", lambda _url: b"protocol-qr-png")

    result = asyncio.run(pool.start_bilibili_qr_auth("admin:device:bilibili"))
    session = pool.get_auth(result["session_id"], "admin:device:bilibili")

    assert result["status"] == "waiting_scan"
    assert result["login_mode"] == "protocol_qr"
    assert result["official_window_open"] is False
    assert result["qr_available"] is True
    assert result["qr_revision"] == 1
    assert session.protocol_secret == "secret-transaction-key-12345678"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret-transaction" not in serialized
    assert "qr_url" not in serialized


def test_bilibili_protocol_poll_handles_confirmation_refresh_and_success(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    challenge_type = load_personification_module(
        "plugin.personification.native_mcp.social_research.bilibili_auth"
    ).BilibiliQrChallenge
    pool = browser_mod.BrowserPool(tmp_path)

    class FakeContext:
        request = object()

        def __init__(self) -> None:
            self.logged_in = False

        async def cookies(self):  # noqa: ANN201
            return [{"name": "SESSDATA"}] if self.logged_in else []

    context = FakeContext()
    states = [86090, 86038, 0]

    async def fake_context(_platform, *, headless=True):  # noqa: ANN001
        assert headless is True
        return context

    async def fake_poll(_request, _key):  # noqa: ANN001
        code = states.pop(0)
        if code == 0:
            context.logged_in = True
        return code

    async def fake_generate(_request):  # noqa: ANN001
        return challenge_type(key="d" * 32, qr_url="https://account.bilibili.com/scan")

    async def fake_close(platform):  # noqa: ANN001
        assert platform == "bilibili"

    monkeypatch.setattr(pool, "context", fake_context)
    monkeypatch.setattr(pool, "close_platform", fake_close)
    monkeypatch.setattr(browser_mod, "poll_challenge", fake_poll)
    monkeypatch.setattr(browser_mod, "generate_challenge", fake_generate)
    monkeypatch.setattr(browser_mod, "render_qr_png", lambda _url: b"fresh-qr")

    session = browser_mod.AuthSession(
        session_id="protocol",
        platform="bilibili",
        owner="owner",
        status="waiting_scan",
        login_mode="protocol_qr",
        protocol_secret="e" * 32,
        qr_png=b"old-qr",
        qr_revision=1,
    )

    async def run():
        first = await pool.refresh_bilibili_qr_auth(session, {"SESSDATA", "DedeUserID"})
        session.last_protocol_poll_at = 0
        session.last_qr_refresh_at = 0
        second = await pool.refresh_bilibili_qr_auth(session, {"SESSDATA", "DedeUserID"})
        session.last_protocol_poll_at = 0
        third = await pool.refresh_bilibili_qr_auth(session, {"SESSDATA", "DedeUserID"})
        return first, second, third

    first, second, third = asyncio.run(run())
    assert first["status"] == "manual_verification_required"
    assert first["verification_kind"] == "device_confirmation"
    assert first["qr_available"] is False
    assert second["status"] == "waiting_scan"
    assert second["qr_available"] is True
    assert second["qr_revision"] == 2
    assert second["qr_refresh_count"] == 1
    assert third["status"] == "success"
    assert third["qr_available"] is False
    assert session.protocol_secret == ""


def test_browser_pool_switches_between_headless_reads_and_visible_official_login(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    launches: list[bool] = []
    channels: list[str] = []

    class FakeContext:
        pages = []

        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class FakeChromium:
        async def launch_persistent_context(self, _profile, *, headless, **_kwargs):  # noqa: ANN001
            launches.append(bool(headless))
            channels.append(str(_kwargs.get("channel") or "bundled"))
            return FakeContext()

    async def run() -> tuple[object, object, object]:
        pool = browser_mod.BrowserPool(tmp_path)
        pool._playwright = type("FakePlaywright", (), {"chromium": FakeChromium()})()
        read_context = await pool.context("bilibili", headless=True)
        reused = await pool.context("bilibili", headless=True)
        login_context = await pool.context("bilibili", headless=False)
        assert read_context.closed is True
        await pool.close_platform("bilibili")
        return read_context, reused, login_context

    read_context, reused, login_context = asyncio.run(run())
    assert read_context is reused
    assert login_context is not read_context
    assert launches == [True, False]
    assert channels == ["chrome", "chrome"]


def test_auth_page_refresh_distinguishes_qr_robot_verification_and_device_confirmation(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    monkeypatch.setattr(browser_mod.BrowserPool, "_looks_like_qr_png", staticmethod(lambda _image: True))
    pool = browser_mod.BrowserPool(tmp_path)

    class FakeLocator:
        first = None

        def __init__(self, page, selector: str) -> None:  # noqa: ANN001
            self.page = page
            self.selector = selector
            self.first = self

        async def count(self) -> int:
            return int(self.selector == "current-qr")

        async def is_visible(self) -> bool:
            return self.selector == "current-qr"

        async def bounding_box(self) -> dict[str, int]:
            return {"width": 156, "height": 156}

        async def screenshot(self, **_kwargs) -> bytes:  # noqa: ANN001
            return self.page.qr

        async def inner_text(self, **_kwargs) -> str:  # noqa: ANN001
            return self.page.text if self.selector == "body" else ""

    class FakePage:
        frames: list[object] = []

        def __init__(self, text: str, qr: bytes = b"qr-one") -> None:
            self.text = text
            self.qr = qr

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(self, selector)

    async def run() -> tuple[object, object, object]:
        qr_session = browser_mod.AuthSession(
            session_id="qr", platform="bilibili", owner="owner", qr_selectors=("current-qr",)
        )
        qr_page = FakePage("扫码登录，也可以切换验证码登录并获取验证码")
        await pool._inspect_auth_page(qr_session, qr_page)
        await pool._inspect_auth_page(qr_session, qr_page)
        assert qr_session.qr_revision == 1
        qr_page.qr = b"qr-two"
        await pool._inspect_auth_page(qr_session, qr_page)

        robot_session = browser_mod.AuthSession(
            session_id="robot", platform="douyin", owner="owner", qr_selectors=("current-qr",)
        )
        await pool._inspect_auth_page(robot_session, FakePage("请完成下列验证 机器人验证"))

        device_session = browser_mod.AuthSession(
            session_id="device", platform="xiaoheihe", owner="owner", qr_selectors=("current-qr",)
        )
        await pool._inspect_auth_page(device_session, FakePage("扫码成功，请在手机上确认"))
        return qr_session, robot_session, device_session

    qr_session, robot_session, device_session = asyncio.run(run())
    assert qr_session.status == "waiting_scan"
    assert qr_session.qr_revision == 2
    assert robot_session.status == "manual_verification_required"
    assert robot_session.verification_kind == "robot_verification"
    assert robot_session.qr_png == b""
    assert device_session.status == "manual_verification_required"
    assert device_session.verification_kind == "device_confirmation"
    assert device_session.qr_png == b""


def test_auth_refresh_reports_closed_official_window_without_reopening_browser(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(
        session_id="closed",
        platform="douyin",
        owner="owner",
        status="waiting_scan",
        official_window_open=True,
        qr_png=b"old-qr",
    )

    result = asyncio.run(pool.refresh_auth(session))

    assert result["status"] == "error"
    assert result["error_code"] == "official_window_closed"
    assert result["official_window_open"] is False
    assert result["qr_available"] is False
    assert pool._playwright is None


def test_robot_verification_switches_embedded_login_to_manual_browser(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(
        session_id="robot-switch",
        platform="douyin",
        owner="owner",
        status="waiting_scan",
        login_url="https://www.douyin.com/",
    )

    class FakePage:
        frames: list[object] = []

        def locator(self, selector):  # noqa: ANN001, ANN201
            class Locator:
                first = None

                def __init__(self) -> None:
                    self.first = self

                async def inner_text(self, **_kwargs) -> str:  # noqa: ANN001
                    return "请完成下列验证 机器人验证" if selector == "body" else ""

            return Locator()

    page = FakePage()
    pool._contexts["douyin"] = type("Context", (), {"pages": [page]})()
    switched: list[str] = []

    async def fake_manual(value):  # noqa: ANN001
        switched.append(value.platform)
        value.login_mode = "manual_browser"
        value.verification_kind = "official_browser_login"
        value.official_window_open = True

    pool._launch_manual_browser = fake_manual

    result = asyncio.run(pool.refresh_auth(session))

    assert switched == ["douyin"]
    assert result["login_mode"] == "manual_browser"
    assert result["verification_kind"] == "official_browser_login"
    assert result["official_window_open"] is True


def test_auth_wait_marks_visible_scan_panel_without_real_qr_as_generation_blocked(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)

    class FakeLocator:
        first = None

        def __init__(self, selector: str) -> None:
            self.selector = selector
            self.first = self

        async def count(self) -> int:
            return 0

        async def is_visible(self) -> bool:
            return False

        async def inner_text(self, **_kwargs) -> str:  # noqa: ANN001
            return "扫码登录 验证码登录" if self.selector == "body" else ""

    class FakePage:
        frames: list[object] = []

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(selector)

        async def wait_for_timeout(self, _milliseconds) -> None:  # noqa: ANN001
            await asyncio.sleep(0.01)

    session = browser_mod.AuthSession(
        session_id="blocked",
        platform="douyin",
        owner="owner",
        qr_selectors=("missing-qr",),
        login_triggered=True,
    )

    asyncio.run(pool._wait_for_auth_surface(session, FakePage(), timeout_seconds=0.05))

    assert session.status == "manual_verification_required"
    assert session.verification_kind == "qr_generation_blocked"
    assert session.qr_png == b""


def test_expired_qr_is_reloaded_and_replaced_inside_same_management_session(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    monkeypatch.setattr(browser_mod.BrowserPool, "_looks_like_qr_png", staticmethod(lambda _image: True))
    pool = browser_mod.BrowserPool(tmp_path)

    class FakeLocator:
        first = None

        def __init__(self, page, selector: str) -> None:  # noqa: ANN001
            self.page = page
            self.selector = selector
            self.first = self

        async def count(self) -> int:
            return int(self.selector == "fresh-qr" and self.page.reloaded)

        async def is_visible(self) -> bool:
            return self.selector == "fresh-qr" and self.page.reloaded

        async def bounding_box(self) -> dict[str, int]:
            return {"width": 180, "height": 180}

        async def screenshot(self, **_kwargs) -> bytes:  # noqa: ANN001
            return b"fresh-qr-png"

        async def inner_text(self, **_kwargs) -> str:  # noqa: ANN001
            if self.selector != "body":
                return ""
            return "扫码登录" if self.page.reloaded else "二维码已过期"

    class FakePage:
        frames: list[object] = []

        def __init__(self) -> None:
            self.reloaded = False
            self.reloads = 0

        def locator(self, selector: str) -> FakeLocator:
            return FakeLocator(self, selector)

        async def reload(self, **_kwargs) -> None:  # noqa: ANN001
            self.reloaded = True
            self.reloads += 1

        async def wait_for_timeout(self, _milliseconds) -> None:  # noqa: ANN001
            return None

    session = browser_mod.AuthSession(
        session_id="renew",
        platform="bilibili",
        owner="owner",
        status="qr_expired",
        qr_selectors=("fresh-qr",),
        login_url="https://passport.bilibili.com/login",
    )
    page = FakePage()

    asyncio.run(pool._renew_expired_qr(session, page))

    assert page.reloads == 1
    assert session.status == "waiting_scan"
    assert session.qr_png == b"fresh-qr-png"
    assert session.qr_revision == 1
    assert session.qr_refresh_count == 1


def test_manual_browser_login_uses_fixed_system_browser_and_private_profile(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"")
    launched: list[list[str]] = []

    class FakeProcess:
        returncode = None

        def poll(self):  # noqa: ANN201
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        def wait(self, **_kwargs):  # noqa: ANN201, ANN003
            return self.returncode

    def fake_popen(args, **_kwargs):  # noqa: ANN001, ANN201
        launched.append([str(value) for value in args])
        return FakeProcess()

    async def run() -> tuple[dict, dict]:
        pool = browser_mod.BrowserPool(tmp_path / "data")
        monkeypatch.setattr(browser_mod.BrowserPool, "_system_browser", staticmethod(lambda: (executable, "chrome")))
        monkeypatch.setattr(browser_mod, "_restrict_private_directory", lambda _path: None)
        monkeypatch.setattr(browser_mod.subprocess, "Popen", fake_popen)
        started = await pool.start_manual_auth(
            "douyin", "admin:device:douyin", "https://www.douyin.com/"
        )
        cancelled = await pool.cancel_auth(started["session_id"], "admin:device:douyin")
        await pool.close()
        return started, cancelled

    started, cancelled = asyncio.run(run())
    assert started["login_mode"] == "manual_browser"
    assert started["verification_kind"] == "official_browser_login"
    assert started["official_window_open"] is True
    assert 1790 <= started["remaining_seconds"] <= 1800
    assert cancelled["status"] == "cancelled"
    assert cancelled["official_window_open"] is False
    assert launched and launched[0][0] == str(executable)
    assert launched[0][-1] == "https://www.douyin.com/"
    assert "--disable-background-mode" in launched[0]
    assert any(value.startswith("--user-data-dir=") for value in launched[0])
    assert "owner" not in started and "native_process" not in started and "login_url" not in started


def test_auth_qr_falls_back_to_headless_with_explicit_interactive_warning(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    monkeypatch.setattr(browser_mod.BrowserPool, "_looks_like_qr_png", staticmethod(lambda _image: True))
    pool = browser_mod.BrowserPool(tmp_path)
    modes: list[bool] = []

    class FakeLocator:
        first = None

        def __init__(self) -> None:
            self.first = self

        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

        async def screenshot(self, **_kwargs) -> bytes:
            return b"png"

    class FakePage:
        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def wait_for_timeout(self, _milliseconds) -> None:  # noqa: ANN001
            return None

        def locator(self, _selector) -> FakeLocator:  # noqa: ANN001
            return FakeLocator()

    async def fake_page(_platform, *, headless=True):  # noqa: ANN001
        modes.append(bool(headless))
        if not headless:
            raise RuntimeError("chromium_unavailable")
        return FakePage()

    pool.page = fake_page
    result = asyncio.run(
        pool.start_auth(
            "bilibili",
            "admin:device:bilibili",
            "https://passport.bilibili.com/login",
            ("qr",),
            (),
        )
    )

    assert modes == [False, True]
    assert result["status"] == "waiting_scan"
    assert result["qr_available"] is True
    assert result["error_code"] == "interactive_window_unavailable"
    assert result["login_mode"] == "headless_page_qr"


def test_auth_status_uses_visible_profile_and_closes_window_after_success(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    session = browser_mod.AuthSession(
        session_id="session",
        platform="bilibili",
        owner="admin:device:bilibili",
        status="manual_verification_required",
    )

    class FakeBrowsers:
        closed: list[str] = []

        def get_auth(self, session_id, owner):  # noqa: ANN001
            assert (session_id, owner) == ("session", "admin:device:bilibili")
            return session

        def public_auth(self, value):  # noqa: ANN001
            return {"status": value.status, "platform": value.platform}

        async def close_platform(self, platform):  # noqa: ANN001
            self.closed.append(platform)

    class FakeAdapter:
        async def authenticated(self, *, interactive=None):  # noqa: ANN001
            assert interactive is True
            return True

    async def run() -> tuple[dict, list[str]]:
        service = service_mod.SocialResearchService(tmp_path)
        browsers = FakeBrowsers()
        service.browsers = browsers
        service.adapters["bilibili"] = FakeAdapter()
        result = await service.auth_status(
            {"session_id": "session", "owner": "admin:device:bilibili"}
        )
        return result, browsers.closed

    result, closed = asyncio.run(run())
    assert result["status"] == "success"
    assert closed == ["bilibili"]


def test_auth_status_refreshes_official_page_when_cookie_is_not_ready(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    session = browser_mod.AuthSession(
        session_id="session",
        platform="douyin",
        owner="admin:device:douyin",
        status="waiting_scan",
    )

    class FakeBrowsers:
        refreshed = False

        def get_auth(self, _session_id, _owner):  # noqa: ANN001
            return session

        async def refresh_auth(self, value):  # noqa: ANN001
            self.refreshed = True
            value.status = "manual_verification_required"
            value.verification_kind = "robot_verification"
            return self.public_auth(value)

        def public_auth(self, value):  # noqa: ANN001
            return {
                "status": value.status,
                "platform": value.platform,
                "verification_kind": value.verification_kind,
            }

    class FakeAdapter:
        async def authenticated(self, *, interactive=None):  # noqa: ANN001
            assert interactive is True
            return False

    async def run() -> tuple[dict, bool]:
        service = service_mod.SocialResearchService(tmp_path)
        browsers = FakeBrowsers()
        service.browsers = browsers
        service.adapters["douyin"] = FakeAdapter()
        result = await service.auth_status(
            {"session_id": "session", "owner": "admin:device:douyin"}
        )
        return result, browsers.refreshed

    result, refreshed = asyncio.run(run())
    assert refreshed is True
    assert result == {
        "status": "manual_verification_required",
        "platform": "douyin",
        "verification_kind": "robot_verification",
    }


def test_auth_status_preserves_headless_qr_context(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    session = browser_mod.AuthSession(
        session_id="headless",
        platform="xiaoheihe",
        owner="admin:device:xiaoheihe",
        status="waiting_scan",
        login_mode="headless_page_qr",
    )

    class FakeBrowsers:
        refreshed = False

        def get_auth(self, _session_id, _owner):  # noqa: ANN001
            return session

        async def refresh_auth(self, value):  # noqa: ANN001
            self.refreshed = True
            return self.public_auth(value)

        def public_auth(self, value):  # noqa: ANN001
            return {"status": value.status, "login_mode": value.login_mode}

    class FakeAdapter:
        async def authenticated(self, *, interactive=None):  # noqa: ANN001
            assert interactive is False
            return False

    async def run() -> tuple[dict, bool]:
        service = service_mod.SocialResearchService(tmp_path)
        browsers = FakeBrowsers()
        service.browsers = browsers
        service.adapters["xiaoheihe"] = FakeAdapter()
        result = await service.auth_status(
            {"session_id": "headless", "owner": "admin:device:xiaoheihe"}
        )
        return result, browsers.refreshed

    result, refreshed = asyncio.run(run())
    assert refreshed is True
    assert result == {"status": "waiting_scan", "login_mode": "headless_page_qr"}


def test_manual_browser_status_waits_for_window_close_before_cookie_detection(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    session = browser_mod.AuthSession(
        session_id="manual",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        verification_kind="official_browser_login",
        login_mode="manual_browser",
        official_window_open=True,
    )

    class FakeBrowsers:
        running = True
        closed: list[str] = []

        def get_auth(self, _session_id, _owner):  # noqa: ANN001
            return session

        def manual_browser_running(self, _session) -> bool:  # noqa: ANN001
            return self.running

        def public_auth(self, value):  # noqa: ANN001
            return {
                "status": value.status,
                "platform": value.platform,
                "verification_kind": value.verification_kind,
                "official_window_open": value.official_window_open,
            }

        async def close_platform(self, platform):  # noqa: ANN001
            self.closed.append(platform)

    class FakeAdapter:
        calls = 0

        async def authenticated(self, *, interactive=None):  # noqa: ANN001
            self.calls += 1
            assert interactive is False
            return True

    async def run() -> tuple[dict, dict, int, list[str]]:
        service = service_mod.SocialResearchService(tmp_path)
        browsers = FakeBrowsers()
        adapter = FakeAdapter()
        service.browsers = browsers
        service.adapters["douyin"] = adapter
        waiting = await service.auth_status(
            {"session_id": "manual", "owner": "admin:device:douyin"}
        )
        browsers.running = False
        completed = await service.auth_status(
            {"session_id": "manual", "owner": "admin:device:douyin"}
        )
        return waiting, completed, adapter.calls, browsers.closed

    waiting, completed, calls, closed = asyncio.run(run())
    assert waiting["verification_kind"] == "official_browser_login"
    assert waiting["official_window_open"] is True
    assert completed["status"] == "success"
    assert completed["official_window_open"] is False
    assert calls == 1
    assert closed == ["douyin"]
