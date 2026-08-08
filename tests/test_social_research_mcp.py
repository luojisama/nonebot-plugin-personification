from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest

from ._loader import load_personification_module


def test_social_video_ref_accepts_only_verified_platform_cdn_https() -> None:
    adapters = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    assert adapters.normalize_platform_video_url(
        "douyin", "https://v3-web.douyinvod.com/video/tos/example.mp4?token=1"
    ).startswith("https://v3-web.douyinvod.com/")
    assert adapters.normalize_platform_video_url(
        "bilibili", "https://xy-videos.bilivideo.com/upgcxcode/example.m4s"
    ).startswith("https://xy-videos.bilivideo.com/")
    assert adapters.normalize_platform_video_url("douyin", "http://v3-web.douyinvod.com/video.mp4") == ""
    assert adapters.normalize_platform_video_url("douyin", "https://evil.example/video.mp4") == ""
    assert adapters.normalize_platform_video_url("tieba", "https://v3-web.douyinvod.com/video.mp4") == ""
    rows = adapters._parse_subtitle_payload(
        json.dumps(
            {
                "body": [
                    {"from": 1.25, "content": "花来"},
                    {"from": 2.5, "content": "修脚抢装撤离"},
                ]
            },
            ensure_ascii=False,
        )
    )
    assert rows == [
        {"text": "花来", "offset_seconds": 1.25},
        {"text": "修脚抢装撤离", "offset_seconds": 2.5},
    ]


def test_social_research_server_exposes_only_read_tools_and_control_is_not_listed(tmp_path: Path) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    project_root = Path(__file__).resolve().parents[2]

    async def run():
        async with compat.McpStdioClient(
            command=sys.executable,
            args=[str(Path(__file__).resolve().parents[1] / "native_mcp" / "social_research" / "entrypoint.py")],
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
    assert status["browser_runtime"]["idle_timeout_seconds"] == 300.0
    assert status["browser_runtime"]["open_contexts"] == []
    assert all(item["state"] == "disabled" for item in status["platforms"].values())
    assert all("enabled" not in item["config"] for item in status["platforms"].values())

    search = next(tool for tool in tools if tool["name"] == "social_content_search")
    research = next(tool for tool in tools if tool["name"] == "research_game_slang")
    assert search["inputSchema"]["properties"]["limit"]["default"] == 10
    assert research["inputSchema"]["properties"]["limit"]["default"] == 10
    assert {"aggregation", "source_groups"} <= set(search["outputSchema"]["properties"])


def test_browser_pool_evicts_only_idle_unprotected_contexts_and_keeps_profile(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")

    class FakeContext:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def run() -> None:
        pool = browser_mod.BrowserPool(tmp_path, idle_timeout_seconds=0.02)
        profile = pool.profile_dir("douyin")
        profile.mkdir(parents=True, exist_ok=True)
        marker = profile / "Cookies"
        marker.write_text("persistent", encoding="utf-8")
        douyin = FakeContext()
        pool._contexts["douyin"] = douyin
        pool._context_headless["douyin"] = True

        async with pool.activity("douyin"):
            await asyncio.sleep(0.04)
            assert douyin.closed is False
        await asyncio.sleep(0.06)
        assert douyin.closed is True
        assert marker.read_text(encoding="utf-8") == "persistent"
        assert "douyin" not in pool._contexts
        assert pool.runtime_status()["diagnostics"][-1]["code"] == "browser_context_idle_evicted"

        tieba = FakeContext()
        pool._contexts["tieba"] = tieba
        pool._context_headless["tieba"] = True
        session = browser_mod.AuthSession(
            session_id="protected",
            platform="tieba",
            owner="admin",
            status="manual_verification_required",
            expires_at=time.time() + 60,
        )
        pool._auth[session.session_id] = session
        async with pool.activity("tieba"):
            pass
        await asyncio.sleep(0.06)
        assert tieba.closed is False
        session.status = "success"
        await asyncio.sleep(0.08)
        assert tieba.closed is True
        await pool.close()

    asyncio.run(run())


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


def test_platform_status_bounds_slow_auth_probe(tmp_path: Path, monkeypatch) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    monkeypatch.setattr(service_mod, "_AUTH_PROBE_TIMEOUT_SECONDS", 0.03)

    class SlowAdapter:
        async def authenticated(self):
            await asyncio.Event().wait()

        def capabilities(self):
            return {"search": True}

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        service._config["xiaoheihe"]["enabled"] = True
        service.adapters["xiaoheihe"] = SlowAdapter()
        try:
            return await service.status()
        finally:
            await service.close()

    started = time.monotonic()
    status = asyncio.run(run())
    assert time.monotonic() - started < 0.5
    platform = status["platforms"]["xiaoheihe"]
    assert platform["state"] == "unavailable"
    assert platform["error_code"] == "platform_timeout"


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


def test_research_game_slang_keeps_selected_detail_when_only_detail_engagement_is_missing(
    tmp_path: Path,
) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    slang = load_personification_module("plugin.personification.core.slang_learning")

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        selected = {
            **_fake_social_item("bilibili", 7),
            "retained": True,
            "filtered_reason": "",
            "source_group_id": "source_selected",
        }

        async def fake_search(_params):  # noqa: ANN001
            return {
                "items": [selected],
                "platform_statuses": {"bilibili": {"state": "ready"}},
                "partial": False,
                "warnings": [],
                "filtered_counts": {},
                "aggregation": {
                    "requested_limit": 1,
                    "returned_count": 1,
                    "source_group_count": 1,
                    "coverage_status": "complete",
                    "satisfies_request": True,
                },
            }

        async def fake_read(_params):  # noqa: ANN001
            return {
                "items": [
                    {
                        **selected,
                        "caption_or_body": "花来是红狼使用高射速武器修脚夺舍后开大撤离的玩法。",
                        "stats": {},
                        "retained": False,
                        "filtered_reason": "low_video_engagement",
                    }
                ]
            }

        service.search = fake_search
        service.read = fake_read
        try:
            packet = await service.research(
                {"term": "花来", "game": "三角洲行动", "context": "群聊", "limit": 1}
            )
            return packet, slang.validate_content_packet(packet)
        finally:
            await service.close()

    packet, validated = asyncio.run(run())
    item = packet["items"][0]
    assert item["retained"] is True
    assert item["filtered_reason"] == ""
    assert item["detail_filtered_reason"] == "low_video_engagement"
    assert item["detail_status"] == "ready"
    assert len(validated["items"]) == 1


def test_research_game_slang_does_not_restore_detail_marketing_risk(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        selected = {**_fake_social_item("bilibili", 8), "retained": True}

        async def fake_search(_params):  # noqa: ANN001
            return {
                "items": [selected],
                "platform_statuses": {"bilibili": {"state": "ready"}},
                "partial": False,
                "warnings": [],
                "filtered_counts": {},
                "aggregation": {},
            }

        async def fake_read(_params):  # noqa: ANN001
            return {
                "items": [
                    {
                        **selected,
                        "retained": False,
                        "filtered_reason": "marketing_risk",
                    }
                ]
            }

        service.search = fake_search
        service.read = fake_read
        try:
            return await service.research(
                {"term": "花来", "game": "三角洲行动", "context": "群聊", "limit": 1}
            )
        finally:
            await service.close()

    packet = asyncio.run(run())
    assert packet["items"][0]["retained"] is False
    assert packet["items"][0]["filtered_reason"] == "marketing_risk"


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
            assert ".hb-bbs-image-text .image-text__content" in script
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

    assert browsers.fake_page.waited_selector.startswith(
        ".hb-bbs-link__content .hb-bbs-image-text .image-text__content"
    )
    assert item["content_id"] == "179364001"
    assert item["caption_or_body"] == "S9赛季正文，不含导航与评论"
    assert item["image_urls"] == [
        "https://cdn.xiaoheihe.cn/post/cover.jpg",
        "https://cdn.xiaoheihe.cn/post/result.jpg",
    ]
    assert item["image_count"] == 2


def test_xiaoheihe_current_detail_boundaries_exclude_navigation_and_split_replies() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    spec = adapters_mod.SPECS["xiaoheihe"]

    assert spec.discussion_selectors == (
        (".link-comment__comment-item .comment-item__content", "comment"),
        (".link-comment__comment-item .children-item__comment-content", "reply"),
    )
    adapter = adapters_mod.PlatformAdapter(spec, object())
    selector = adapter._detail_selector("https://xiaoheihe.cn/app/bbs/link/179364001")
    script = adapter._detail_script("https://xiaoheihe.cn/app/bbs/link/179364001")

    assert ".hb-bbs-link__content" in selector
    assert ".image-text__content" in selector
    assert "document.body.innerText" not in script
    assert ".hb-bbs-link__content,.hb-bbs-post" in script


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


def test_douyin_search_reads_waterfall_cards_without_content_anchors() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        async def evaluate_all(self, _script, _options):  # noqa: ANN001, ANN201
            return [
                {
                    "contentId": "7506916630866218267",
                    "contentKind": "video",
                    "title": "夺舍流红狼",
                    "author": "红狼玩家",
                    "text": "夺舍流红狼 00:57",
                    "metricText": ["12.5万", "321"],
                    "cover": "https://p3-sign.douyinpic.com/video.jpg?card_type=153",
                    "images": [{"url": "https://p3-sign.douyinpic.com/video.jpg?card_type=153"}],
                },
                {
                    "contentId": "7668144591689864549",
                    "contentKind": "note",
                    "title": "花来配装图文",
                    "author": "三角洲玩家",
                    "text": "花来配装图文 图文",
                    "metricText": ["88"],
                    "cover": "https://p3-sign.douyinpic.com/note.jpg?card_type=303",
                    "images": [{"url": "https://p3-sign.douyinpic.com/note.jpg?card_type=303"}],
                },
                {"contentId": "1", "contentKind": "video", "text": "相关搜索", "skip": True},
                {"contentId": "2", "contentKind": "", "text": "类型不明"},
            ]

    class FakePage:
        def __init__(self) -> None:
            self.waited_selector = ""
            self.locator_selector = ""
            self.closed = False

        async def goto(self, url, **_kwargs):  # noqa: ANN001, ANN201
            assert url.startswith("https://www.douyin.com/search/")
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def evaluate(self, _script):  # noqa: ANN001, ANN201
            return {"title": "抖音搜索", "body": "三角洲行动 花来"}

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        def locator(self, selector):  # noqa: ANN001, ANN201
            self.locator_selector = selector
            return FakeLocator()

        async def close(self) -> None:
            self.closed = True

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def fresh_page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["douyin"], browsers)
    items = asyncio.run(adapter.search("三角洲行动 花来", limit=10, timeout_seconds=5))

    assert browsers.fake_page.waited_selector == '[id^="waterfall_item_"]'
    assert browsers.fake_page.locator_selector == '[id^="waterfall_item_"]'
    assert browsers.fake_page.closed is True
    assert [(item["content_id"], item["content_type"]) for item in items] == [
        ("7506916630866218267", "video"),
        ("7668144591689864549", "article"),
    ]
    assert items[0]["canonical_url"] == "https://www.douyin.com/video/7506916630866218267"
    assert items[1]["canonical_url"] == "https://www.douyin.com/note/7668144591689864549"
    assert items[1]["author"]["display_name"] == "三角洲玩家"


def test_tieba_search_groups_repeated_post_links_by_thread_card() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        async def evaluate_all(self, _script, _options):  # noqa: ANN001, ANN201
            return [
                {"href": "https://tieba.baidu.com/p/9876543210", "title": "花来是什么", "text": "正文一", "metricText": ["25"]},
                {"href": "https://tieba.baidu.com/p/9876543210", "title": "回复入口", "text": "重复链接", "metricText": ["25"]},
                {"href": "https://tieba.baidu.com/p/9876543211", "title": "红狼夺舍流", "text": "正文二", "metricText": ["18"]},
                {"href": "https://tieba.baidu.com/f?kw=test", "title": "贴吧首页", "text": "非内容"},
            ]

    class FakePage:
        def __init__(self) -> None:
            self.waited_selector = ""

        async def goto(self, url, **_kwargs):  # noqa: ANN001, ANN201
            assert url.startswith("https://tieba.baidu.com/f/search/res?")
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def evaluate(self, _script):  # noqa: ANN001, ANN201
            return {"title": "贴吧搜索", "body": "三角洲行动"}

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        def locator(self, _selector):  # noqa: ANN001, ANN201
            return FakeLocator()

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["tieba"], browsers)
    items = asyncio.run(adapter.search("三角洲行动 花来", limit=10, timeout_seconds=5))

    assert browsers.fake_page.waited_selector == ".virtual-list-item"
    assert [item["content_id"] for item in items] == ["9876543210", "9876543211"]
    assert all(item["content_type"] == "post" for item in items)


def test_douyin_note_detail_uses_main_content_images_and_comment_boundaries() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def all_inner_texts(self):  # noqa: ANN201
            if self.selector == '[data-e2e="comment-item"]':
                return ["头 甲 枪 胸挂 背包 花来"]
            if "reply" in self.selector:
                return ["这就是红狼夺舍流"]
            return []

    class FakePage:
        def __init__(self) -> None:
            self.evaluate_calls = 0
            self.waited_selector = ""

        async def goto(self, url, **_kwargs):  # noqa: ANN001, ANN201
            assert url == "https://www.douyin.com/note/7668144591689864549"
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        async def evaluate(self, script):  # noqa: ANN001, ANN201
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {"title": "抖音", "body": "图文详情"}
            assert "const isNote = true" in script
            assert "comment-item" in script
            return {
                "title": "花来配装",
                "description": "红狼使用高射速武器修脚，保留头甲后夺舍撤离",
                "cover": "https://p3-sign.douyinpic.com/note-1.jpg",
                "images": [
                    {"url": "https://p3-sign.douyinpic.com/note-1.jpg"},
                    {"url": "https://p3-sign.douyinpic.com/note-2.jpg"},
                ],
                "author": "三角洲玩家",
                "body": "导航和相关推荐不应使用",
            }

        def locator(self, selector):  # noqa: ANN001, ANN201
            return FakeLocator(selector)

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["douyin"], browsers)
    item = asyncio.run(
        adapter.read(
            content_id="",
            url="https://www.douyin.com/note/7668144591689864549",
            include=["caption", "comments", "replies"],
            comment_limit=30,
            danmaku_limit=0,
            timeout_seconds=5,
        )
    )

    assert browsers.fake_page.waited_selector == "main"
    assert item["content_type"] == "article"
    assert item["caption_or_body"] == "红狼使用高射速武器修脚，保留头甲后夺舍撤离"
    assert len(item["image_urls"]) == 2
    assert [entry["type"] for entry in item["discussion"]] == ["comment", "reply"]


def test_tieba_detail_uses_current_post_and_comment_containers() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")

    class FakeResponse:
        status = 200

    class FakeLocator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def all_inner_texts(self):  # noqa: ANN201
            return ["楼中楼讨论花来出处"] if self.selector == ".pb-comment-item" else []

    class FakePage:
        def __init__(self) -> None:
            self.evaluate_calls = 0
            self.waited_selector = ""

        async def goto(self, url, **_kwargs):  # noqa: ANN001, ANN201
            assert url == "https://tieba.baidu.com/p/9876543210"
            return FakeResponse()

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

        async def wait_for_selector(self, selector, **_kwargs):  # noqa: ANN001, ANN201
            self.waited_selector = selector
            return object()

        async def evaluate(self, script):  # noqa: ANN001, ANN201
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {"title": "贴吧", "body": "帖子详情"}
            assert ".pb-content-wrap" in script
            assert ".pb-comment-list" in script
            return {
                "title": "三角洲花来玩法讨论",
                "description": "使用肉伤弹修脚并保留装备耐久",
                "cover": "https://imgsa.baidu.com/forum/post-1.jpg",
                "images": [{"url": "https://imgsa.baidu.com/forum/post-1.jpg"}],
                "author": "吧友",
                "body": "导航内容不应使用",
            }

        def locator(self, selector):  # noqa: ANN001, ANN201
            return FakeLocator(selector)

    class FakeBrowsers:
        def __init__(self) -> None:
            self.fake_page = FakePage()

        async def page(self, _platform):  # noqa: ANN001, ANN201
            return self.fake_page

    browsers = FakeBrowsers()
    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["tieba"], browsers)
    item = asyncio.run(
        adapter.read(
            content_id="",
            url="https://tieba.baidu.com/p/9876543210",
            include=["caption", "comments", "replies"],
            comment_limit=30,
            danmaku_limit=0,
            timeout_seconds=5,
        )
    )

    assert browsers.fake_page.waited_selector == ".pb-content-wrap"
    assert item["caption_or_body"] == "使用肉伤弹修脚并保留装备耐久"
    assert item["image_urls"] == ["https://imgsa.baidu.com/forum/post-1.jpg"]
    assert [entry["text"] for entry in item["discussion"]] == ["楼中楼讨论花来出处"]


def test_article_and_post_media_keep_six_opaque_image_refs(tmp_path: Path) -> None:
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    service = service_mod.SocialResearchService(tmp_path)
    item = {
        "content_type": "article",
        "cover_ref": "https://p3-sign.douyinpic.com/cover.jpg",
        "image_urls": [f"https://p3-sign.douyinpic.com/note-{index}.jpg" for index in range(1, 8)],
        "image_count": 7,
    }

    service._register_item_media("douyin", item)

    assert len(item["image_refs"]) == 6
    assert item["cover_ref"] == item["image_refs"][0]
    assert item["image_count"] == 7


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
    assert adapters_mod.SPECS["douyin"].auth_cookie_names == frozenset(
        {"sessionid", "sessionid_ss", "sid_guard"}
    )
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
    avatar = Image.new("RGB", (240, 240), "white")
    avatar_draw = ImageDraw.Draw(avatar)
    avatar_draw.ellipse((18, 18, 222, 222), fill="#f2a6b8", outline="#242936", width=8)
    avatar_draw.polygon(((20, 70), (85, 10), (125, 88)), fill="#7b2339")
    avatar_draw.ellipse((62, 65, 118, 132), fill="#f7fbff", outline="#162033", width=7)
    avatar_draw.ellipse((78, 74, 104, 125), fill="#54a9ee", outline="#162033", width=5)
    avatar_draw.arc((80, 120, 190, 205), 15, 150, fill="#7b2339", width=8)
    avatar_output = BytesIO()
    avatar.save(avatar_output, format="PNG")
    real_qr = protocol.render_qr_png(
        "https://account.bilibili.com/h5/account-h5/auth/scan-web?qrcode_key=" + "1" * 32
    )

    assert browser_mod.BrowserPool._looks_like_qr_png(output.getvalue()) is False
    assert browser_mod.BrowserPool._looks_like_qr_png(avatar_output.getvalue()) is False
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


def test_platform_adapter_routes_webui_interactive_to_fixed_official_surface() -> None:
    adapters_mod = load_personification_module("plugin.personification.native_mcp.social_research.adapters")
    captured: dict = {}

    class FakeBrowsers:
        async def start_interactive_auth(
            self, platform, owner, login_url, allowed_hosts, qr_selectors, login_trigger_selectors
        ):  # noqa: ANN001, ANN201
            captured.update(
                platform=platform,
                owner=owner,
                login_url=login_url,
                allowed_hosts=allowed_hosts,
                qr_selectors=qr_selectors,
                login_trigger_selectors=login_trigger_selectors,
            )
            return {"login_mode": "webui_interactive", "status": "manual_verification_required"}

    adapter = adapters_mod.PlatformAdapter(adapters_mod.SPECS["tieba"], FakeBrowsers())
    result = asyncio.run(adapter.start_auth("admin:device:tieba", mode="webui_interactive"))

    assert result["login_mode"] == "webui_interactive"
    assert captured["platform"] == "tieba"
    assert captured["owner"] == "admin:device:tieba"
    assert captured["login_url"] == "https://tieba.baidu.com/"
    assert captured["allowed_hosts"] == ("tieba.baidu.com", "baidu.com", "www.baidu.com")
    assert captured["qr_selectors"] == adapters_mod.SPECS["tieba"].qr_selectors


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


def test_webui_interactive_auth_relays_only_bounded_human_input(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    mouse_events: list[tuple] = []
    key_events: list[tuple] = []

    class FakeMouse:
        async def click(self, x, y, **kwargs):  # noqa: ANN001, ANN201
            mouse_events.append(("click", x, y, kwargs.get("delay")))

        async def move(self, x, y):  # noqa: ANN001, ANN201
            mouse_events.append(("move", x, y))

        async def down(self):  # noqa: ANN201
            mouse_events.append(("down",))

        async def up(self):  # noqa: ANN201
            mouse_events.append(("up",))

        async def wheel(self, x, y):  # noqa: ANN001, ANN201
            mouse_events.append(("wheel", x, y))

    class FakeKeyboard:
        async def insert_text(self, value):  # noqa: ANN001, ANN201
            key_events.append(("type", value))

        async def press(self, value):  # noqa: ANN001, ANN201
            key_events.append(("key", value))

    class FakePage:
        url = "https://passport.douyin.com/login/?secret=must-not-leak"
        viewport_size = {"width": 1280, "height": 900}
        mouse = FakeMouse()
        keyboard = FakeKeyboard()
        screenshots = 0

        async def screenshot(self, **kwargs):  # noqa: ANN001, ANN201
            assert kwargs == {"type": "jpeg", "quality": 60}
            self.screenshots += 1
            return b"safe-jpeg"

        async def wait_for_timeout(self, _milliseconds):  # noqa: ANN001, ANN201
            return None

    page = FakePage()
    pool._contexts["douyin"] = type("Context", (), {"pages": [page]})()
    session = browser_mod.AuthSession(
        session_id="interactive",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    async def run():
        first = await pool.interactive_frame("interactive", "admin:device:douyin")
        second = await pool.interactive_frame(
            "interactive",
            "admin:device:douyin",
            after_revision=first["interactive_frame_revision"],
        )
        await pool.interactive_action(
            "interactive", "admin:device:douyin", {"type": "click", "x": 100, "y": 200}
        )
        await pool.interactive_action(
            "interactive",
            "admin:device:douyin",
            {
                "type": "drag",
                "points": [
                    {"x": 100, "y": 300, "t": 0},
                    {"x": 130, "y": 301, "t": 20},
                    {"x": 180, "y": 299, "t": 40},
                ],
            },
        )
        await pool.interactive_action(
            "interactive", "admin:device:douyin", {"type": "type", "text": "123456"}
        )
        await pool.interactive_action(
            "interactive", "admin:device:douyin", {"type": "key", "key": "Enter"}
        )
        await pool.interactive_action(
            "interactive", "admin:device:douyin", {"type": "scroll", "delta_y": 800}
        )
        return first, second

    first, second = asyncio.run(run())
    assert base64.b64decode(first["data_base64"]) == b"safe-jpeg"
    assert second["interactive_frame_revision"] == 1
    assert second["changed"] is False
    assert "data_base64" not in second
    assert page.screenshots == 1
    assert first["interactive_display_url"] == "https://passport.douyin.com/"
    assert "secret" not in json.dumps(first)
    assert ("click", 100.0, 200.0, 60) in mouse_events
    assert ("down",) in mouse_events and ("up",) in mouse_events
    assert ("wheel", 0, 800.0) in mouse_events
    assert key_events == [("type", "123456"), ("key", "Enter")]

    with pytest.raises(KeyError):
        asyncio.run(pool.interactive_frame("interactive", "other-admin:device:douyin"))
    with pytest.raises(ValueError):
        asyncio.run(
            pool.interactive_action(
                "interactive", "admin:device:douyin", {"type": "click", "x": 5000, "y": 1}
            )
        )
    with pytest.raises(ValueError):
        asyncio.run(
            pool.interactive_action(
                "interactive", "admin:device:douyin", {"type": "key", "key": "Control+L"}
            )
        )


def test_webui_interactive_pointer_stream_moves_before_release_and_is_idempotent(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    mouse_events: list[tuple] = []

    class FakeMouse:
        async def move(self, x, y):  # noqa: ANN001, ANN201
            mouse_events.append(("move", x, y))

        async def down(self):  # noqa: ANN201
            mouse_events.append(("down",))

        async def up(self):  # noqa: ANN201
            mouse_events.append(("up",))

    class FakePage:
        url = "https://www.douyin.com/"
        viewport_size = {"width": 1280, "height": 900}
        mouse = FakeMouse()

    pool._contexts["douyin"] = type("Context", (), {"pages": [FakePage()]})()
    session = browser_mod.AuthSession(
        session_id="pointer-stream",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    async def run():
        started = await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_start", "gesture_id": "gesture_stream01", "seq": 0, "x": 100, "y": 200},
        )
        assert started["interactive_pointer_active"] is True
        assert mouse_events == [("move", 100.0, 200.0), ("down",)]

        moved = await pool.interactive_action(
            session.session_id,
            session.owner,
            {
                "type": "pointer_move",
                "gesture_id": "gesture_stream01",
                "seq": 1,
                "points": [{"x": 140, "y": 201}, {"x": 180, "y": 202}],
            },
        )
        assert moved["action_applied"] is True
        assert mouse_events[-2:] == [("move", 140.0, 201.0), ("move", 180.0, 202.0)]

        duplicate = await pool.interactive_action(
            session.session_id,
            session.owner,
            {
                "type": "pointer_move",
                "gesture_id": "gesture_stream01",
                "seq": 1,
                "points": [{"x": 200, "y": 202}],
            },
        )
        mismatch = await pool.interactive_action(
            session.session_id,
            session.owner,
            {
                "type": "pointer_move",
                "gesture_id": "gesture_other001",
                "seq": 2,
                "points": [{"x": 220, "y": 202}],
            },
        )
        assert duplicate["action_duplicate"] is True
        assert mismatch["interactive_pointer_error_code"] == "interactive_pointer_gesture_mismatch"
        assert mouse_events[-1] == ("move", 180.0, 202.0)

        ended = await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_end", "gesture_id": "gesture_stream01", "seq": 2, "x": 260, "y": 203},
        )
        repeated_end = await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_end", "gesture_id": "gesture_stream01", "seq": 2, "x": 260, "y": 203},
        )
        return ended, repeated_end

    ended, repeated_end = asyncio.run(run())
    assert ended["interactive_pointer_active"] is False
    assert repeated_end["action_duplicate"] is True
    assert mouse_events[-2:] == [("move", 260.0, 203.0), ("up",)]


def test_webui_interactive_pointer_end_releases_after_move_failure(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    mouse_events: list[tuple] = []

    class FakeMouse:
        async def move(self, x, y):  # noqa: ANN001, ANN201
            mouse_events.append(("move", x, y))
            if x == 999:
                raise RuntimeError("move failed")

        async def down(self):  # noqa: ANN201
            mouse_events.append(("down",))

        async def up(self):  # noqa: ANN201
            mouse_events.append(("up",))

    page = type(
        "Page",
        (),
        {"url": "https://www.douyin.com/", "viewport_size": {"width": 1280, "height": 900}, "mouse": FakeMouse()},
    )()
    pool._contexts["douyin"] = type("Context", (), {"pages": [page]})()
    session = browser_mod.AuthSession(
        session_id="pointer-failure",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    async def run():
        await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_start", "gesture_id": "gesture_failure1", "seq": 0, "x": 100, "y": 100},
        )
        with pytest.raises(RuntimeError, match="move failed"):
            await pool.interactive_action(
                session.session_id,
                session.owner,
                {"type": "pointer_end", "gesture_id": "gesture_failure1", "seq": 1, "x": 999, "y": 100},
            )

    asyncio.run(run())
    assert mouse_events[-1] == ("up",)
    assert session.interactive_pointer_down is False


def test_webui_interactive_pointer_timeout_and_platform_close_release_mouse(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    monkeypatch.setattr(browser_mod, "_INTERACTIVE_POINTER_TIMEOUT_SECONDS", 0.02)
    pool = browser_mod.BrowserPool(tmp_path)
    mouse_events: list[tuple] = []
    closed: list[bool] = []

    class FakeMouse:
        async def move(self, x, y):  # noqa: ANN001, ANN201
            mouse_events.append(("move", x, y))

        async def down(self):  # noqa: ANN201
            mouse_events.append(("down",))

        async def up(self):  # noqa: ANN201
            mouse_events.append(("up",))

    page = type(
        "Page",
        (),
        {"url": "https://www.douyin.com/", "viewport_size": {"width": 1280, "height": 900}, "mouse": FakeMouse()},
    )()

    class FakeContext:
        pages = [page]

        async def close(self):  # noqa: ANN201
            closed.append(True)

    pool._contexts["douyin"] = FakeContext()
    session = browser_mod.AuthSession(
        session_id="pointer-timeout",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    async def run():
        await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_start", "gesture_id": "gesture_timeout1", "seq": 0, "x": 100, "y": 100},
        )
        await asyncio.sleep(0.05)
        assert session.interactive_pointer_down is False
        assert session.interactive_pointer_error_code == "interactive_pointer_timeout"

        await pool.interactive_action(
            session.session_id,
            session.owner,
            {"type": "pointer_start", "gesture_id": "gesture_timeout2", "seq": 0, "x": 120, "y": 100},
        )
        await pool.close_platform("douyin")

    asyncio.run(run())
    assert mouse_events.count(("up",)) == 2
    assert closed == [True]
    assert session.interactive_pointer_down is False


def test_webui_interactive_pointer_move_rejects_oversized_batch(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(
        session_id="pointer-size",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    with pytest.raises(ValueError, match="pointer points"):
        asyncio.run(
            pool.interactive_action(
                session.session_id,
                session.owner,
                {
                    "type": "pointer_move",
                    "gesture_id": "gesture_size0001",
                    "seq": 1,
                    "points": [{"x": index, "y": 10} for index in range(7)],
                },
            )
        )


def test_webui_interactive_auth_starts_page_in_background(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        class FakePage:
            url = "https://www.douyin.com/"

            async def goto(self, *_args, **_kwargs) -> None:
                entered.set()
                await release.wait()

            async def wait_for_timeout(self, _milliseconds) -> None:  # noqa: ANN001
                return None

        async def fake_page(_platform, *, headless=True):  # noqa: ANN001
            assert headless is True
            return FakePage()

        async def fake_wait(session, _page, timeout_seconds=10.0):  # noqa: ANN001
            assert timeout_seconds == 10.0
            session.status = "manual_verification_required"
            session.verification_kind = "official_page"

        pool.page = fake_page
        pool._wait_for_auth_surface = fake_wait
        result = await pool.start_interactive_auth(
            "douyin",
            "admin:device:douyin",
            "https://www.douyin.com/",
            ("douyin.com",),
            (),
            (),
        )
        assert result["status"] == "starting"
        assert result["interactive_available"] is False
        await entered.wait()
        session = pool.get_auth(result["session_id"], "admin:device:douyin")
        assert session.interactive_start_task is not None
        assert session.interactive_start_task.done() is False
        release.set()
        await session.interactive_start_task
        return pool.public_auth(session)

    finished = asyncio.run(run())
    assert finished["status"] == "manual_verification_required"
    assert finished["interactive_available"] is True


def test_scanned_qr_avatar_transitions_to_device_confirmation(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(
        session_id="scanned",
        platform="douyin",
        owner="admin:device:douyin",
        status="waiting_scan",
        login_mode="headless_page_qr",
        qr_png=b"original-qr",
        qr_missing_since=time.time() - 1.0,
    )

    async def no_qr(_page, _selectors):  # noqa: ANN001
        return b""

    async def no_text(_page):  # noqa: ANN001
        return ""

    pool._capture_qr = no_qr
    pool._page_text = no_text
    asyncio.run(pool._inspect_auth_page(session, object()))

    assert session.status == "manual_verification_required"
    assert session.verification_kind == "device_confirmation"
    assert session.qr_png == b""


def test_douyin_device_confirmation_accepts_sid_guard_and_finishes_login(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    service = service_mod.SocialResearchService(tmp_path)
    pool = service.browsers
    session = browser_mod.AuthSession(
        session_id="douyin-confirmed",
        platform="douyin",
        owner="admin:device:douyin",
        status="waiting_scan",
        login_mode="webui_interactive",
        qr_png=b"original-qr",
        qr_missing_since=time.time() - 1.0,
        official_window_open=True,
    )

    class FakeContext:
        pages: list[object] = []
        closed = False

        async def cookies(self):  # noqa: ANN201
            return [
                {
                    "name": "sid_guard",
                    "value": "authenticated-session-guard",
                    "domain": ".douyin.com",
                }
            ]

        async def close(self) -> None:
            self.closed = True

    context = FakeContext()
    pool._contexts["douyin"] = context
    pool._context_headless["douyin"] = True
    pool._auth[session.session_id] = session

    async def no_qr(_page, _selectors):  # noqa: ANN001
        return b""

    async def no_text(_page):  # noqa: ANN001
        return ""

    async def run():
        pool._capture_qr = no_qr
        pool._page_text = no_text
        await pool._inspect_auth_page(session, object())
        assert session.verification_kind == "device_confirmation"
        return await service.auth_status(
            {"session_id": session.session_id, "owner": session.owner}
        )

    result = asyncio.run(run())

    assert result["status"] == "success"
    assert result["verification_kind"] == ""
    assert result["qr_available"] is False
    assert result["official_window_open"] is False
    assert context.closed is True


def test_interactive_frame_returns_cached_frame_while_page_is_busy(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    session = browser_mod.AuthSession(
        session_id="busy-frame",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
        interactive_frame=b"cached-jpeg",
        interactive_frame_revision=3,
    )
    pool._auth[session.session_id] = session

    async def run():
        async with session.interactive_lock:
            return await pool.interactive_frame(
                session.session_id,
                session.owner,
                after_revision=2,
            )

    result = asyncio.run(run())
    assert result["stale"] is True
    assert result["changed"] is True
    assert base64.b64decode(result["data_base64"]) == b"cached-jpeg"


def test_interactive_drag_replay_time_is_capped(tmp_path: Path, monkeypatch) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    delays: list[float] = []

    class FakeMouse:
        async def move(self, *_args) -> None:
            return None

        async def down(self) -> None:
            return None

        async def up(self) -> None:
            return None

    class FakePage:
        url = "https://www.douyin.com/"
        viewport_size = {"width": 1280, "height": 900}
        mouse = FakeMouse()

        async def wait_for_timeout(self, _milliseconds) -> None:  # noqa: ANN001
            return None

    async def fake_sleep(seconds):  # noqa: ANN001
        delays.append(float(seconds))

    monkeypatch.setattr(browser_mod.asyncio, "sleep", fake_sleep)
    pool._contexts["douyin"] = type("Context", (), {"pages": [FakePage()]})()
    session = browser_mod.AuthSession(
        session_id="long-drag",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("douyin.com",),
    )
    pool._auth[session.session_id] = session

    asyncio.run(
        pool.interactive_action(
            session.session_id,
            session.owner,
            {
                "type": "drag",
                "points": [
                    {"x": 10, "y": 10, "t": 0},
                    {"x": 300, "y": 100, "t": 2500},
                    {"x": 600, "y": 120, "t": 5000},
                ],
            },
        )
    )
    assert 0 < sum(delays) <= browser_mod._INTERACTIVE_DRAG_REPLAY_MAX_SECONDS


def test_webui_interactive_auth_fails_closed_after_cross_platform_redirect(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    page = type(
        "Page",
        (),
        {"url": "https://evil.example/login", "viewport_size": {"width": 1280, "height": 900}},
    )()
    pool._contexts["tieba"] = type("Context", (), {"pages": [page]})()
    session = browser_mod.AuthSession(
        session_id="outside",
        platform="tieba",
        owner="admin:device:tieba",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_allowed_hosts=("baidu.com",),
    )
    pool._auth[session.session_id] = session

    with pytest.raises(RuntimeError, match="interactive_page_outside_platform"):
        asyncio.run(pool.interactive_frame("outside", "admin:device:tieba"))
    assert session.status == "error"
    assert session.official_window_open is False
    assert session.interactive_frame == b""


def test_platform_profile_allows_only_one_active_admin_auth_session(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    pool = browser_mod.BrowserPool(tmp_path)
    first = browser_mod.AuthSession(
        session_id="first",
        platform="xiaoheihe",
        owner="admin-a:device:xiaoheihe",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_frame=b"frame-a",
    )
    second = browser_mod.AuthSession(
        session_id="second",
        platform="xiaoheihe",
        owner="admin-b:device:xiaoheihe",
        status="waiting_scan",
        interactive_frame=b"frame-b",
    )
    unrelated = browser_mod.AuthSession(
        session_id="other-platform",
        platform="tieba",
        owner="admin-a:device:tieba",
        status="waiting_scan",
    )
    pool._auth = {item.session_id: item for item in (first, second, unrelated)}

    asyncio.run(pool._supersede_auth("xiaoheihe", "admin-c:device:xiaoheihe"))

    assert first.status == second.status == "cancelled"
    assert first.interactive_frame == second.interactive_frame == b""
    assert first.official_window_open is False
    assert unrelated.status == "waiting_scan"


def test_webui_interactive_finish_reuses_profile_and_closes_browser_context(tmp_path: Path) -> None:
    browser_mod = load_personification_module("plugin.personification.native_mcp.social_research.browser")
    service_mod = load_personification_module("plugin.personification.native_mcp.social_research.service")
    session = browser_mod.AuthSession(
        session_id="finish",
        platform="douyin",
        owner="admin:device:douyin",
        status="manual_verification_required",
        login_mode="webui_interactive",
        official_window_open=True,
        interactive_frame=b"sensitive-pixels",
    )

    class FakeBrowsers:
        closed: list[str] = []

        def get_auth(self, session_id, owner):  # noqa: ANN001
            assert (session_id, owner) == ("finish", "admin:device:douyin")
            return session

        def public_auth(self, value):  # noqa: ANN001
            return {
                "status": value.status,
                "platform": value.platform,
                "login_mode": value.login_mode,
                "official_window_open": value.official_window_open,
            }

        async def close_platform(self, platform):  # noqa: ANN001
            self.closed.append(platform)

    class FakeAdapter:
        async def authenticated(self, *, interactive=None):  # noqa: ANN001
            assert interactive is False
            return True

    async def run():
        service = service_mod.SocialResearchService(tmp_path)
        browsers = FakeBrowsers()
        service.browsers = browsers
        service.adapters["douyin"] = FakeAdapter()
        result = await service.auth_finish(
            {"session_id": "finish", "owner": "admin:device:douyin"}
        )
        return result, browsers.closed

    result, closed = asyncio.run(run())
    assert result["status"] == "success"
    assert result["official_window_open"] is False
    assert session.interactive_frame == b""
    assert closed == ["douyin"]


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
