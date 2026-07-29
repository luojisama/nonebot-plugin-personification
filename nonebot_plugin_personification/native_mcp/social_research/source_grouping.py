from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Any


def _clean(value: Any, limit: int = 2000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _content_key(item: dict[str, Any]) -> str:
    return f"{str(item.get('platform') or '').strip()}:{str(item.get('content_id') or '').strip()}"


def _similarity(left: str, right: str) -> float:
    a = _clean(left).casefold()
    b = _clean(right).casefold()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def cluster_content_sources(packet: dict[str, Any]) -> dict[str, str]:
    items = [item for item in list(packet.get("items") or []) if isinstance(item, dict)]
    keys = [_content_key(item) for item in items]
    parents = {key: key for key in keys if key != ":"}

    def find(value: str) -> str:
        while parents[value] != value:
            parents[value] = parents[parents[value]]
            value = parents[value]
        return value

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return
        winner, loser = sorted((root_left, root_right))
        parents[loser] = winner

    for left_index, left in enumerate(items):
        left_key = keys[left_index]
        if left_key not in parents:
            continue
        for right_index in range(left_index + 1, len(items)):
            right = items[right_index]
            right_key = keys[right_index]
            if right_key not in parents:
                continue
            if left_key == right_key:
                union(left_key, right_key)
                continue
            explicit_left = left.get("repost_of") or left.get("external_source_url")
            explicit_right = right.get("repost_of") or right.get("external_source_url")
            if explicit_left and explicit_left == explicit_right:
                union(left_key, right_key)
                continue
            if left.get("media_fingerprint") and left["media_fingerprint"] == right.get("media_fingerprint"):
                union(left_key, right_key)
                continue
            if left.get("content_fingerprint") and left["content_fingerprint"] == right.get("content_fingerprint"):
                union(left_key, right_key)
                continue
            left_body = f"{left.get('title', '')} {left.get('caption_or_body', '')}"
            right_body = f"{right.get('title', '')} {right.get('caption_or_body', '')}"
            if min(len(_clean(left_body)), len(_clean(right_body))) >= 40 and _similarity(left_body, right_body) >= 0.92:
                union(left_key, right_key)

    result: dict[str, str] = {}
    for key in parents:
        root = find(key)
        digest = hashlib.sha256(root.encode("utf-8")).hexdigest()[:24]
        result[key] = f"source_{digest}"
    return result


def attach_source_group_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = cluster_content_sources({"items": items})
    result: list[dict[str, Any]] = []
    for raw in items:
        item = dict(raw)
        item["source_group_id"] = groups.get(_content_key(item), "")
        result.append(item)
    return result


def _rank_key(item: dict[str, Any]) -> tuple[float, float, str, str]:
    return (
        -float(item.get("quality_score", 0) or 0),
        -float(item.get("published_at", 0) or 0),
        str(item.get("platform") or ""),
        str(item.get("content_id") or ""),
    )


def select_multi_source_items(
    items: list[dict[str, Any]],
    *,
    platforms: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    maximum = max(1, int(limit))
    ranked = sorted((dict(item) for item in items), key=_rank_key)
    selected: list[dict[str, Any]] = []
    selected_keys: set[str] = set()
    selected_groups: set[str] = set()

    platform_heads: list[dict[str, Any]] = []
    head_groups: set[str] = set()
    for platform in platforms:
        head = next(
            (
                item
                for item in ranked
                if item.get("platform") == platform
                and _content_key(item) not in selected_keys
                and str(item.get("source_group_id") or "") not in head_groups
            ),
            None,
        )
        if head is not None:
            platform_heads.append(head)
            group_id = str(head.get("source_group_id") or "")
            if group_id:
                head_groups.add(group_id)
    if len(platform_heads) > maximum:
        platform_heads = sorted(platform_heads, key=_rank_key)[:maximum]
    for item in platform_heads:
        key = _content_key(item)
        group_id = str(item.get("source_group_id") or "")
        selected.append(item)
        selected_keys.add(key)
        if group_id:
            selected_groups.add(group_id)

    for unseen_only in (True, False):
        for item in ranked:
            if len(selected) >= maximum:
                break
            key = _content_key(item)
            group_id = str(item.get("source_group_id") or "")
            if key in selected_keys or (unseen_only and group_id in selected_groups):
                continue
            selected.append(item)
            selected_keys.add(key)
            if group_id:
                selected_groups.add(group_id)
        if len(selected) >= maximum:
            break
    return selected


def build_source_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for item in items:
        group_id = str(item.get("source_group_id") or "")
        if not group_id:
            continue
        if group_id not in grouped:
            grouped[group_id] = []
            order.append(group_id)
        grouped[group_id].append(item)
    result: list[dict[str, Any]] = []
    for group_id in order:
        members = grouped[group_id]
        result.append(
            {
                "group_id": group_id,
                "member_count": len(members),
                "platforms": list(dict.fromkeys(str(item.get("platform") or "") for item in members)),
                "members": [
                    {
                        "platform": str(item.get("platform") or ""),
                        "content_id": str(item.get("content_id") or ""),
                        "canonical_url": str(item.get("canonical_url") or ""),
                    }
                    for item in members
                ],
            }
        )
    return result


__all__ = [
    "attach_source_group_ids",
    "build_source_groups",
    "cluster_content_sources",
    "select_multi_source_items",
]
