from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from .adapters import SPECS, PlatformAdapter, build_adapters
from .browser import BrowserPool
from .cache import PacketCache
from .covers import CoverRegistry
from .models import (
    ContentPacket,
    DEFAULT_PLATFORM_CONFIG,
    PLATFORMS,
    QUALITY_MODES,
    apply_quality_filter,
    clean_text,
    validate_platforms,
)
from .source_grouping import attach_source_group_ids, build_source_groups, select_multi_source_items


_SAFE_OPERATION_CODES = {
    "chromium_unavailable",
    "login_page_unavailable",
    "login_required",
    "manual_verification_required",
    "manual_browser_start_failed",
    "official_window_closed",
    "platform_disabled",
    "playwright_unavailable",
    "platform_request_failed",
    "platform_timeout",
    "risk_controlled",
    "qr_expired",
    "qr_refresh_failed",
    "system_browser_unavailable",
    "bilibili_login_state_missing",
    "bilibili_qr_generate_failed",
    "bilibili_qr_poll_failed",
    "bilibili_qr_refresh_limit",
    "bilibili_qr_session_invalid",
    "bilibili_qr_unknown_state",
    "qrcode_encoder_failed",
    "qrcode_encoder_unavailable",
    "no_enabled_platform",
    "detail_content_unavailable",
}


def _safe_operation_code(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "platform_timeout"
    value = str(exc).strip()
    return value if value in _SAFE_OPERATION_CODES else "platform_request_failed"


class SocialResearchService:
    def __init__(self, root: str | Path | None = None) -> None:
        resolved = Path(root or os.environ.get("PERSONIFICATION_SOCIAL_DATA_DIR") or "data/personification/mcp/social_platform")
        self.root = resolved.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.browsers = BrowserPool(self.root)
        self.adapters = build_adapters(self.browsers)
        self.cache = PacketCache(self.root / "cache")
        self.covers = CoverRegistry(self.root)
        self.config_path = self.root / "config.json"
        self._config_lock = asyncio.Lock()
        self._config = self._load_config()

    def _load_config(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            raw = {}
        result: dict[str, dict[str, Any]] = {}
        for platform in PLATFORMS:
            current = raw.get(platform) if isinstance(raw, dict) and isinstance(raw.get(platform), dict) else {}
            result[platform] = {**DEFAULT_PLATFORM_CONFIG, **current, "enabled": bool(current.get("enabled", False))}
        return result

    async def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = str(params.get("platform") or "")
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        enabled = params.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("enabled must be a JSON boolean")
        supplied = params.get("config") if isinstance(params.get("config"), dict) else {}
        allowed = set(DEFAULT_PLATFORM_CONFIG)
        unknown = set(supplied) - allowed
        if unknown:
            raise ValueError("platform config contains unsupported fields")
        config = {**self._config[platform], **supplied, "enabled": enabled}
        mode = str(config.get("quality_mode") or "")
        if mode not in QUALITY_MODES:
            raise ValueError("quality_mode is invalid")
        config["marketing_threshold"] = min(1.0, max(0.0, float(config.get("marketing_threshold", 0.75))))
        for key in ("min_play_count", "min_comment_count", "min_reply_count", "max_results", "comment_limit", "danmaku_limit", "cache_ttl_seconds", "request_timeout_seconds"):
            config[key] = max(0, int(config.get(key, DEFAULT_PLATFORM_CONFIG[key]) or 0))
        config["max_results"] = min(50, max(1, config["max_results"]))
        config["comment_limit"] = min(200, config["comment_limit"])
        config["danmaku_limit"] = min(500, config["danmaku_limit"])
        config["cache_ttl_seconds"] = min(86400, max(60, config["cache_ttl_seconds"]))
        config["request_timeout_seconds"] = min(60, max(3, config["request_timeout_seconds"]))
        async with self._config_lock:
            self._config[platform] = config
            temporary = self.config_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._config, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, self.config_path)
        return await self.status()

    async def _platform_status(self, platform: str) -> dict[str, Any]:
        adapter = self.adapters[platform]
        config = self._config[platform]
        authenticated = False
        error_code = ""
        if config.get("enabled"):
            try:
                authenticated = await adapter.authenticated()
            except Exception as exc:
                error_code = _safe_operation_code(exc)
        state = "disabled" if not config.get("enabled") else "ready" if authenticated else "login_required"
        if error_code:
            state = "unavailable"
        return {
            "platform": platform,
            "enabled": bool(config.get("enabled")),
            "authenticated": authenticated,
            "state": state,
            "error_code": error_code,
            "capabilities": adapter.capabilities(),
            # ``enabled`` is a control-plane field, not platform configuration.
            # Keeping it out of this nested object prevents WebUI status data
            # from being replayed as an unsupported configure field.
            "config": {
                key: config.get(key, default)
                for key, default in DEFAULT_PLATFORM_CONFIG.items()
            },
        }

    async def status(self) -> dict[str, Any]:
        results = await asyncio.gather(*(self._platform_status(platform) for platform in PLATFORMS))
        return {"schema_version": 1, "platforms": {item["platform"]: item for item in results}}

    async def health(self) -> dict[str, Any]:
        status = await self.status()
        return {
            **status,
            "ok": any(item["state"] == "ready" for item in status["platforms"].values()),
            "chromium_required": True,
            "install_command": "python -m playwright install chromium",
        }

    async def auth_start(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = str(params.get("platform") or "")
        owner = clean_text(params.get("owner"), 200)
        mode = str(params.get("mode") or "embedded_qr")
        if platform not in PLATFORMS or not owner or mode not in {"embedded_qr", "manual_browser"}:
            raise ValueError("platform, owner and a valid auth mode are required")
        return await self.adapters[platform].start_auth(owner, mode=mode)

    async def auth_status(self, params: dict[str, Any]) -> dict[str, Any]:
        owner = clean_text(params.get("owner"), 200)
        session = self.browsers.get_auth(str(params.get("session_id") or ""), owner)
        if session.login_mode == "protocol_qr":
            if session.status == "expired":
                await self.browsers.expire_auth(session)
                return self.browsers.public_auth(session)
            return await self.browsers.refresh_bilibili_qr_auth(
                session,
                set(SPECS["bilibili"].auth_cookie_names),
            )
        if session.login_mode == "manual_browser" and session.status != "expired":
            if self.browsers.manual_browser_running(session):
                return self.browsers.public_auth(session)
            session.official_window_open = False
            if session.verification_kind == "official_browser_login":
                await asyncio.sleep(0.5)
            try:
                authenticated = await self.adapters[session.platform].authenticated(interactive=False)
            except Exception:
                authenticated = False
            if authenticated:
                session.status = "success"
                session.error_code = ""
                session.verification_kind = ""
                await self.browsers.close_platform(session.platform)
            else:
                session.status = "manual_verification_required"
                session.verification_kind = "manual_login_incomplete"
            return self.browsers.public_auth(session)
        if session.status in {
            "starting", "waiting_scan", "manual_verification_required", "risk_controlled", "qr_expired"
        }:
            try:
                authenticated = await self.adapters[session.platform].authenticated(
                    interactive=session.login_mode == "embedded_qr"
                )
            except Exception:
                authenticated = False
            if authenticated:
                session.status = "success"
                session.qr_png = b""
                session.error_code = ""
                session.verification_kind = ""
                session.official_window_open = False
                await self.browsers.close_platform(session.platform)
            else:
                await self.browsers.refresh_auth(session)
        elif session.status == "expired":
            await self.browsers.expire_auth(session)
        return self.browsers.public_auth(session)

    async def auth_qrcode(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.browsers.auth_qrcode(
            str(params.get("session_id") or ""),
            clean_text(params.get("owner"), 200),
        )

    async def auth_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        result = await self.browsers.cancel_auth(
            str(params.get("session_id") or ""),
            clean_text(params.get("owner"), 200),
        )
        await self.browsers.close_platform(str(result.get("platform") or ""))
        return result

    async def auth_logout(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = str(params.get("platform") or "")
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        await self.browsers.logout(platform)
        return {"platform": platform, "state": "login_required", "authenticated": False}

    async def _ready_adapter(self, platform: str) -> tuple[PlatformAdapter, dict[str, Any]]:
        config = self._config[platform]
        if not config.get("enabled"):
            raise RuntimeError("platform_disabled")
        adapter = self.adapters[platform]
        if not await adapter.authenticated():
            raise RuntimeError("login_required")
        return adapter, config

    async def _search_platform(self, platform: str, query: str, limit: int, quality_mode: str) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        started = time.monotonic()
        adapter, base_config = await self._ready_adapter(platform)
        config = {**base_config, "quality_mode": quality_mode}
        rows = await asyncio.wait_for(
            adapter.search(query, limit=limit, timeout_seconds=float(config["request_timeout_seconds"])),
            timeout=float(config["request_timeout_seconds"]) + 2,
        )
        filtered = [apply_quality_filter(item, config) for item in rows]
        retained = [item for item in filtered if item["retained"]]
        for item in retained:
            item["cover_ref"] = self.covers.register(platform, str(item.get("cover_ref") or ""))
        return retained, {
            "state": "ready",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "candidate_count": len(rows),
            "retained_count": len(retained),
        }, len(filtered) - len(retained)

    async def search(self, params: dict[str, Any]) -> dict[str, Any]:
        query = clean_text(params.get("query"), 200)
        if not query:
            raise ValueError("query is required")
        if params.get("platforms") is None:
            platforms = [platform for platform in PLATFORMS if self._config[platform].get("enabled")]
            if not platforms:
                raise RuntimeError("no_enabled_platform")
        else:
            platforms = validate_platforms(params.get("platforms"))
        raw_content_types = params.get("content_types")
        if raw_content_types is None:
            content_types = ["video", "article", "post"]
        elif isinstance(raw_content_types, list):
            content_types = list(dict.fromkeys(str(item) for item in raw_content_types))
            if not content_types or any(item not in {"video", "article", "post"} for item in content_types):
                raise ValueError("content_types is invalid")
        else:
            raise ValueError("content_types must be an array")
        limit = min(50, max(1, int(params.get("limit", 10) or 10)))
        quality_mode = str(params.get("quality_mode") or "balanced")
        if quality_mode not in QUALITY_MODES:
            raise ValueError("quality_mode is invalid")
        cache_key = json.dumps(
            ["search", "multi_source_v1", query, platforms, content_types, limit, quality_mode, self._config],
            ensure_ascii=False,
            sort_keys=True,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached
        per_platform_budget = max(3, 2 * math.ceil(limit / len(platforms)))
        results = await asyncio.gather(
            *(
                self._search_platform(
                    platform,
                    query,
                    min(int(self._config[platform].get("max_results", 10) or 10), per_platform_budget),
                    quality_mode,
                )
                for platform in platforms
            ),
            return_exceptions=True,
        )
        packet = ContentPacket(ttl_seconds=min(self._config[p]["cache_ttl_seconds"] for p in platforms))
        candidates: list[dict[str, Any]] = []
        successful_platforms: list[str] = []
        per_platform_counts: dict[str, dict[str, int]] = {
            platform: {"candidates": 0, "filtered": 0, "returned": 0}
            for platform in platforms
        }
        for platform, result in zip(platforms, results):
            if isinstance(result, BaseException):
                code = _safe_operation_code(result)
                packet.platform_statuses[platform] = {"state": code, "error_code": code}
                packet.partial = True
                continue
            items, status, filtered_count = result
            successful_platforms.append(platform)
            retained_types = [item for item in items if str(item.get("content_type") or "") in content_types]
            candidates.extend(retained_types)
            packet.platform_statuses[platform] = status
            packet.filtered_counts[platform] = filtered_count + len(items) - len(retained_types)
            per_platform_counts[platform]["candidates"] = int(status.get("candidate_count", len(items)) or 0)
            per_platform_counts[platform]["filtered"] = packet.filtered_counts[platform]

        best_by_content: dict[tuple[str, str], dict[str, Any]] = {}
        for item in candidates:
            key = (str(item.get("platform") or ""), str(item.get("content_id") or ""))
            previous = best_by_content.get(key)
            if previous is None or float(item.get("quality_score", 0) or 0) > float(previous.get("quality_score", 0) or 0):
                best_by_content[key] = item
        grouped_candidates = attach_source_group_ids(list(best_by_content.values()))
        packet.items = select_multi_source_items(grouped_candidates, platforms=platforms, limit=limit)
        packet.source_groups = build_source_groups(packet.items)
        covered_platforms = list(dict.fromkeys(str(item.get("platform") or "") for item in packet.items))
        for platform in covered_platforms:
            returned = sum(1 for item in packet.items if item.get("platform") == platform)
            per_platform_counts[platform]["returned"] = returned
            packet.platform_statuses[platform]["returned_count"] = returned
        required_groups = min(2, limit)
        required_platforms = min(2, len(platforms))
        satisfies_request = bool(
            packet.items
            and len(packet.source_groups) >= required_groups
            and len(covered_platforms) >= required_platforms
        )
        coverage_status = "empty" if not packet.items else "complete" if satisfies_request and not packet.partial else "degraded"
        if packet.items and not satisfies_request:
            packet.warnings.append("social_coverage_degraded")
        packet.aggregation = {
            "requested_limit": limit,
            "candidate_count": sum(item["candidates"] for item in per_platform_counts.values()),
            "returned_count": len(packet.items),
            "source_group_count": len(packet.source_groups),
            "selected_platforms": list(platforms),
            "successful_platforms": successful_platforms,
            "covered_platforms": covered_platforms,
            "per_platform_counts": per_platform_counts,
            "coverage_status": coverage_status,
            "satisfies_request": satisfies_request,
        }
        value = packet.to_dict()
        self.cache.set(cache_key, value, packet.ttl_seconds)
        return value

    async def read(self, params: dict[str, Any]) -> dict[str, Any]:
        platform = str(params.get("platform") or "")
        if platform not in PLATFORMS:
            raise ValueError("unsupported platform")
        adapter, config = await self._ready_adapter(platform)
        include = params.get("include") if isinstance(params.get("include"), list) else ["caption", "comments", "replies", "danmaku"]
        include = [str(item) for item in include]
        comment_limit = min(200, max(0, int(params.get("comment_limit", config["comment_limit"]) or 0)))
        danmaku_limit = min(500, max(0, int(params.get("danmaku_limit", config["danmaku_limit"]) or 0)))
        content_id = clean_text(params.get("content_id"), 300)
        url = clean_text(params.get("url"), 1000)
        cache_key = json.dumps(
            ["read", "opaque_cover_v1", platform, content_id, url, include, comment_limit, danmaku_limit],
            ensure_ascii=False,
            sort_keys=True,
        )
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached["cache_hit"] = True
            return cached
        raw = await asyncio.wait_for(
            adapter.read(
                content_id=content_id,
                url=url,
                include=include,
                comment_limit=comment_limit,
                danmaku_limit=danmaku_limit,
                timeout_seconds=float(config["request_timeout_seconds"]),
            ),
            timeout=float(config["request_timeout_seconds"]) + 3,
        )
        item = apply_quality_filter(raw, config)
        item["cover_ref"] = self.covers.register(platform, str(item.get("cover_ref") or ""))
        packet = ContentPacket(
            items=[item] if item["retained"] or str(config.get("quality_mode")) == "ranking_only" else [],
            platform_statuses={platform: {"state": "ready"}},
            filtered_counts={platform: int(not item["retained"])},
            ttl_seconds=int(config["cache_ttl_seconds"]),
        )
        value = packet.to_dict()
        self.cache.set(cache_key, value, packet.ttl_seconds)
        return value

    async def research(self, params: dict[str, Any]) -> dict[str, Any]:
        term = clean_text(params.get("term"), 100)
        context = clean_text(params.get("context"), 1000)
        game = clean_text(params.get("game"), 100)
        depth = str(params.get("depth") or "auto")
        if not term or not context or depth not in {"auto", "deep"}:
            raise ValueError("term, context and a valid depth are required")
        limit = min(50, max(1, int(params.get("limit", 10) or 10)))
        query = " ".join(value for value in (game, term) if value)
        search_started = time.monotonic()
        search_packet = await self.search(
            {"query": query, "limit": limit, "quality_mode": "balanced"}
        )
        search_elapsed_ms = int((time.monotonic() - search_started) * 1000)
        candidates = list(search_packet.get("items") or [])[:limit]
        comment_limit = 80 if depth == "deep" else 30
        danmaku_limit = 200 if depth == "deep" else 80

        async def read_detail(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str, int]:
            started = time.monotonic()
            try:
                result = await self.read(
                    {
                        "platform": item["platform"],
                        "url": item["canonical_url"],
                        "include": ["caption", "comments", "replies", "danmaku"],
                        "comment_limit": comment_limit,
                        "danmaku_limit": danmaku_limit,
                    }
                )
                return result, "ready", int((time.monotonic() - started) * 1000)
            except Exception as exc:
                return None, _safe_operation_code(exc), int((time.monotonic() - started) * 1000)

        detail_started = time.monotonic()
        read_results = await asyncio.gather(
            *(
                read_detail(item)
                for item in candidates
            ),
        )
        detail_elapsed_ms = int((time.monotonic() - detail_started) * 1000)
        packet = ContentPacket(
            platform_statuses=dict(search_packet.get("platform_statuses") or {}),
            partial=bool(search_packet.get("partial")),
            warnings=list(search_packet.get("warnings") or []),
            filtered_counts=dict(search_packet.get("filtered_counts") or {}),
            aggregation=dict(search_packet.get("aggregation") or {}),
        )
        ready_details = 0
        for candidate, (result, detail_code, item_elapsed_ms) in zip(candidates, read_results):
            if result is None or not list(result.get("items") or []):
                packet.partial = True
                fallback = {
                    **candidate,
                    "detail_status": "detail_content_unavailable",
                    "detail_error_code": detail_code,
                    "detail_elapsed_ms": item_elapsed_ms,
                }
                packet.items.append(fallback)
                packet.warnings.append(
                    f"detail_content_unavailable:{candidate.get('platform', '')}:{candidate.get('content_id', '')}"
                )
                continue
            detailed = dict(list(result.get("items") or [])[0])
            detailed["source_group_id"] = str(candidate.get("source_group_id") or "")
            detailed["detail_status"] = "ready"
            detailed["detail_elapsed_ms"] = item_elapsed_ms
            packet.items.append(detailed)
            ready_details += 1
        packet.items = packet.items[:limit]
        packet.source_groups = build_source_groups(packet.items)
        packet.aggregation.update(
            {
                "returned_count": len(packet.items),
                "source_group_count": len(packet.source_groups),
                "coverage_status": (
                    "empty"
                    if not packet.items
                    else "degraded"
                    if packet.partial
                    else str(packet.aggregation.get("coverage_status") or "complete")
                ),
                "stages": {
                    "search": {
                        "status": str(
                            (search_packet.get("aggregation") or {}).get("coverage_status")
                            or "empty"
                        ),
                        "elapsed_ms": search_elapsed_ms,
                        "cache_hit": bool(search_packet.get("cache_hit", False)),
                        "returned_count": len(candidates),
                    },
                    "detail": {
                        "status": (
                            "empty"
                            if not candidates
                            else "complete"
                            if ready_details == len(candidates)
                            else "degraded"
                        ),
                        "elapsed_ms": detail_elapsed_ms,
                        "requested_count": len(candidates),
                        "ready_count": ready_details,
                        "unavailable_count": len(candidates) - ready_details,
                    },
                },
            }
        )
        return packet.to_dict()

    async def close(self) -> None:
        await self.browsers.close()

    def cover_resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.covers.resolve(clean_text(params.get("cover_ref"), 100))


__all__ = ["SocialResearchService"]
