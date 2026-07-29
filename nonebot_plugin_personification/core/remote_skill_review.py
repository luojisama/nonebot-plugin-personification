from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from .data_store import get_data_store
from ..skill_runtime.source_resolver import (
    get_skill_cache_dir,
    parse_skill_sources,
    resolve_skill_sources,
)


_STORE_NAME = "remote_skill_reviews"
_VALID_STATUS = {"pending", "approved", "rejected"}
_CONTENT_DIGEST_LENGTH = 64


def _content_digest(source: dict[str, Any]) -> str:
    value = str(source.get("content_digest") or "").strip().lower()
    if len(value) != _CONTENT_DIGEST_LENGTH or any(ch not in "0123456789abcdef" for ch in value):
        return ""
    return value


def _source_identity(source: dict[str, Any]) -> dict[str, str]:
    return {
        "name": str(source.get("name") or "").strip(),
        "source": str(source.get("source") or "").strip(),
        "ref": str(source.get("ref") or "").strip(),
        "subdir": str(source.get("subdir") or "").strip(),
        "kind": str(source.get("kind") or "auto").strip().lower() or "auto",
    }


def _source_key(identity: dict[str, str]) -> str:
    payload = json.dumps(
        {
            "source": identity.get("source", ""),
            "ref": identity.get("ref", ""),
            "subdir": identity.get("subdir", ""),
            "kind": identity.get("kind", "auto"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _normalize_store(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"items": {}}
    items = raw.get("items")
    if not isinstance(items, dict):
        raw["items"] = {}
    return raw


def _sync_current_items(raw_sources: Any, logger: Any) -> list[dict[str, Any]]:
    parsed = [
        item
        for item in parse_skill_sources(raw_sources, logger)
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    now_ts = time.time()

    def _mutate(current: Any) -> dict[str, Any]:
        store = _normalize_store(current)
        items = store["items"]
        for source in parsed:
            identity = _source_identity(source)
            key = _source_key(identity)
            existing = items.get(key)
            if not isinstance(existing, dict):
                existing = {}
            status = str(existing.get("status") or "pending").strip().lower()
            if status not in _VALID_STATUS:
                status = "pending"
            item = dict(existing)
            item.update(identity)
            observed_digest = _content_digest(source)
            if observed_digest:
                previous_digest = _content_digest(existing)
                item["content_digest"] = observed_digest
                item["digest_seen_at"] = now_ts
                approved_digest = str(existing.get("approved_digest") or "").strip().lower()
                if status == "approved" and approved_digest != observed_digest:
                    status = "pending"
                    item["approval_invalidated_at"] = now_ts
                    item["previous_content_digest"] = previous_digest
            elif _content_digest(existing):
                item["content_digest"] = _content_digest(existing)
            if status == "approved" and not _content_digest({"content_digest": existing.get("approved_digest")}):
                status = "pending"
                item["approval_invalidated_at"] = now_ts
            item["key"] = key
            item["status"] = status
            item["last_seen_at"] = now_ts
            item["created_at"] = float(existing.get("created_at") or now_ts)
            item["updated_at"] = float(existing.get("updated_at") or item["created_at"])
            items[key] = item
        return store

    store = get_data_store().mutate_sync(_STORE_NAME, _mutate)
    items = store.get("items", {}) if isinstance(store, dict) else {}
    current_items: list[dict[str, Any]] = []
    for source in parsed:
        key = _source_key(_source_identity(source))
        item = items.get(key)
        if isinstance(item, dict):
            current_items.append(dict(item))
    return current_items


def list_remote_skill_reviews(
    raw_sources: Any,
    logger: Any,
    *,
    status: str = "",
) -> list[dict[str, Any]]:
    items = _sync_current_items(raw_sources, logger)
    expected_status = str(status or "").strip().lower()
    if expected_status in _VALID_STATUS:
        items = [
            item
            for item in items
            if str(item.get("status") or "").strip().lower() == expected_status
        ]
    return items


def _read_stored_items(raw_sources: Any, logger: Any) -> list[dict[str, Any]]:
    parsed = [
        item
        for item in parse_skill_sources(raw_sources, logger)
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    store = _normalize_store(get_data_store().load_sync(_STORE_NAME))
    items = store.get("items", {})
    return [
        dict(item)
        for source in parsed
        if isinstance((item := items.get(_source_key(_source_identity(source)))), dict)
    ]


def filter_approved_remote_sources(
    raw_sources: Any,
    logger: Any,
    *,
    require_confirmation: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    parsed = [
        item
        for item in parse_skill_sources(raw_sources, logger)
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    if not require_confirmation:
        return parsed, []

    current_items = {
        str(item.get("key") or ""): item
        for item in _sync_current_items(raw_sources, logger)
        if isinstance(item, dict)
    }
    approved: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for source in parsed:
        key = _source_key(_source_identity(source))
        review = current_items.get(key, {})
        digest = _content_digest(source)
        approved_digest = str(review.get("approved_digest") or "").strip().lower()
        if (
            str(review.get("status") or "pending").strip().lower() == "approved"
            and digest
            and approved_digest == digest
        ):
            approved.append(source)
        else:
            pending.append(dict(review) if isinstance(review, dict) else {"key": key, **source})
    return approved, pending


def review_remote_skill_sources(
    raw_sources: Any,
    logger: Any,
    *,
    selector: str,
    status: str,
    operator: str = "",
) -> tuple[int, list[dict[str, Any]]]:
    expected_status = str(status or "").strip().lower()
    if expected_status not in {"approved", "rejected", "pending"}:
        return 0, []

    parsed = [
        item
        for item in parse_skill_sources(raw_sources, logger)
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    observed_digests = {
        _source_key(_source_identity(source)): _content_digest(source)
        for source in parsed
    }
    current_items = _read_stored_items(parsed, logger)
    token = str(selector or "").strip()
    lowered = token.lower()
    if lowered in {"", "pending", "待审批"}:
        targets = [
            item
            for item in current_items
            if str(item.get("status") or "pending").strip().lower() == "pending"
        ]
    elif lowered in {"all", "全部"}:
        targets = current_items
    else:
        targets = []
        for item in current_items:
            key = str(item.get("key") or "")
            name = str(item.get("name") or "")
            source = str(item.get("source") or "")
            if (
                key.startswith(token)
                or name == token
                or token in name
                or token in source
            ):
                targets.append(item)

    if not targets:
        return 0, []

    now_ts = time.time()
    target_digests = {
        str(item.get("key") or ""): observed_digests.get(str(item.get("key") or ""), "")
        for item in targets
        if str(item.get("key") or "")
    }
    if expected_status == "approved":
        target_digests = {
            key: digest
            for key, digest in target_digests.items()
            if digest
        }
    target_keys = set(target_digests)
    if not target_keys:
        return 0, []
    updated_keys: set[str] = set()

    def _mutate(current: Any) -> dict[str, Any]:
        store = _normalize_store(current)
        items = store["items"]
        for key in target_keys:
            item = items.get(key)
            if not isinstance(item, dict):
                continue
            if expected_status == "approved" and _content_digest(item) != target_digests.get(key):
                continue
            item["status"] = expected_status
            item["updated_at"] = now_ts
            item["reviewed_at"] = now_ts
            item["reviewed_by"] = operator
            if expected_status == "approved":
                item["approved_digest"] = _content_digest(item)
            else:
                item["approved_digest"] = ""
            items[key] = item
            updated_keys.add(key)
        return store

    get_data_store().mutate_sync(_STORE_NAME, _mutate)
    refreshed = _read_stored_items(parsed, logger)
    refreshed_map = {
        str(item.get("key") or ""): item
        for item in refreshed
        if isinstance(item, dict)
    }
    matched = [dict(refreshed_map[key]) for key in updated_keys if key in refreshed_map]
    return len(matched), matched


def get_remote_skill_review_stats(raw_sources: Any, logger: Any) -> dict[str, int]:
    items = list_remote_skill_reviews(raw_sources, logger)
    stats = {"total": len(items), "pending": 0, "approved": 0, "rejected": 0}
    for item in items:
        status = str(item.get("status") or "pending").strip().lower()
        if status in stats:
            stats[status] += 1
    return stats


async def prepare_remote_skill_reviews(
    raw_sources: Any,
    plugin_config: Any,
    logger: Any,
    *,
    data_dir: Path,
) -> list[dict[str, Any]]:
    parsed = [
        item
        for item in parse_skill_sources(raw_sources, logger)
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]

    class _SourceConfigProxy:
        def __getattr__(self, name: str) -> Any:
            if name == "personification_skill_sources":
                return parsed
            return getattr(plugin_config, name)

    resolved = await resolve_skill_sources(
        plugin_config=_SourceConfigProxy(),
        logger=logger,
        cache_dir=get_skill_cache_dir(plugin_config, Path(data_dir)),
    )
    digest_by_key = {
        _source_key(_source_identity(item.source)): item.content_digest
        for item in resolved
    }
    prepared: list[dict[str, Any]] = []
    for source in parsed:
        enriched = dict(source)
        content_digest = digest_by_key.get(_source_key(_source_identity(source)), "")
        if content_digest:
            enriched["content_digest"] = content_digest
        prepared.append(enriched)
    _sync_current_items(prepared, logger)
    return prepared


__all__ = [
    "filter_approved_remote_sources",
    "get_remote_skill_review_stats",
    "list_remote_skill_reviews",
    "prepare_remote_skill_reviews",
    "review_remote_skill_sources",
]
