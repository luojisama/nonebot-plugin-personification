from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from .data_store import get_data_store
from ..skill_runtime.source_resolver import parse_skill_sources


_STORE_NAME = "remote_skill_reviews"
_VALID_STATUS = {"pending", "approved", "rejected"}


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
        if str(review.get("status") or "pending").strip().lower() == "approved":
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

    current_items = list_remote_skill_reviews(raw_sources, logger)
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
    target_keys = {str(item.get("key") or "") for item in targets if str(item.get("key") or "")}

    def _mutate(current: Any) -> dict[str, Any]:
        store = _normalize_store(current)
        items = store["items"]
        for key in target_keys:
            item = items.get(key)
            if not isinstance(item, dict):
                continue
            item["status"] = expected_status
            item["updated_at"] = now_ts
            item["reviewed_at"] = now_ts
            item["reviewed_by"] = operator
            items[key] = item
        return store

    get_data_store().mutate_sync(_STORE_NAME, _mutate)
    refreshed = list_remote_skill_reviews(raw_sources, logger)
    refreshed_map = {
        str(item.get("key") or ""): item
        for item in refreshed
        if isinstance(item, dict)
    }
    matched = [dict(refreshed_map[key]) for key in target_keys if key in refreshed_map]
    return len(matched), matched


def get_remote_skill_review_stats(raw_sources: Any, logger: Any) -> dict[str, int]:
    items = list_remote_skill_reviews(raw_sources, logger)
    stats = {"total": len(items), "pending": 0, "approved": 0, "rejected": 0}
    for item in items:
        status = str(item.get("status") or "pending").strip().lower()
        if status in stats:
            stats[status] += 1
    return stats


__all__ = [
    "filter_approved_remote_sources",
    "get_remote_skill_review_stats",
    "list_remote_skill_reviews",
    "review_remote_skill_sources",
]
