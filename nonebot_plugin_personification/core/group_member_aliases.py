from __future__ import annotations

import re
import time
from typing import Any

from .data_store import get_data_store


_NS_GROUP_MEMBER_ALIASES = "group_member_aliases"
_MAX_ALIAS_COUNT = 12
_MAX_ALIAS_CHARS = 32
_MAX_NOTE_CHARS = 160


def _norm_id(value: Any) -> str:
    return str(value or "").strip()


def _now() -> float:
    return time.time()


def normalize_aliases(value: Any) -> list[str]:
    """Normalize admin-maintained group aliases.

    This is explicit admin data sanitation, not normal-chat semantic matching.
    """
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, str):
        raw_items = re.split(r"[\n,，、;；|/]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            if isinstance(item, str) and re.search(r"[\n,，、;；|/]+", item):
                raw_items.extend(re.split(r"[\n,，、;；|/]+", item))
            else:
                raw_items.append(item)
    else:
        raw_items = [value]

    aliases: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        alias = re.sub(r"\s+", " ", str(raw or "").strip())
        alias = alias.strip("「」『』[]()（）<>《》\"'`")
        if not alias:
            continue
        alias = alias[:_MAX_ALIAS_CHARS]
        key = alias.casefold()
        if key in seen:
            continue
        seen.add(key)
        aliases.append(alias)
        if len(aliases) >= _MAX_ALIAS_COUNT:
            break
    return aliases


def _normalize_entry(user_id: str, raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    aliases = normalize_aliases(data.get("aliases", []))
    return {
        "user_id": _norm_id(user_id),
        "aliases": aliases,
        "note": str(data.get("note", "") or "").strip()[:_MAX_NOTE_CHARS],
        "updated_at": float(data.get("updated_at", 0) or 0),
        "updated_by": str(data.get("updated_by", "") or "").strip()[:64],
    }


def _load_all() -> dict[str, Any]:
    data = get_data_store().load_sync(_NS_GROUP_MEMBER_ALIASES)
    return data if isinstance(data, dict) else {}


def list_group_member_aliases(group_id: str) -> dict[str, dict[str, Any]]:
    gid = _norm_id(group_id)
    if not gid:
        return {}
    group_data = _load_all().get(gid, {})
    if not isinstance(group_data, dict):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for uid, raw in group_data.items():
        user_id = _norm_id(uid)
        if not user_id:
            continue
        entry = _normalize_entry(user_id, raw)
        if entry["aliases"] or entry["note"]:
            entries[user_id] = entry
    return entries


def get_group_member_alias_entry(group_id: str, user_id: str) -> dict[str, Any]:
    uid = _norm_id(user_id)
    if not uid:
        return {"user_id": "", "aliases": [], "note": "", "updated_at": 0.0, "updated_by": ""}
    return list_group_member_aliases(group_id).get(
        uid,
        {"user_id": uid, "aliases": [], "note": "", "updated_at": 0.0, "updated_by": ""},
    )


def set_group_member_aliases(
    group_id: str,
    user_id: str,
    aliases: Any,
    *,
    note: str = "",
    updated_by: str = "",
) -> dict[str, Any]:
    gid = _norm_id(group_id)
    uid = _norm_id(user_id)
    if not gid or not uid:
        raise ValueError("group_id and user_id are required")
    normalized_aliases = normalize_aliases(aliases)
    normalized_note = str(note or "").strip()[:_MAX_NOTE_CHARS]
    actor = str(updated_by or "").strip()[:64]
    entry = {
        "user_id": uid,
        "aliases": normalized_aliases,
        "note": normalized_note,
        "updated_at": _now(),
        "updated_by": actor,
    }

    def _mutate(current: Any) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        group_data = data.get(gid)
        if not isinstance(group_data, dict):
            group_data = {}
            data[gid] = group_data
        if normalized_aliases or normalized_note:
            group_data[uid] = dict(entry)
        else:
            group_data.pop(uid, None)
        if not group_data:
            data.pop(gid, None)
        return data

    get_data_store().mutate_sync(_NS_GROUP_MEMBER_ALIASES, _mutate)
    return entry


def delete_group_member_aliases(group_id: str, user_id: str) -> bool:
    gid = _norm_id(group_id)
    uid = _norm_id(user_id)
    if not gid or not uid:
        return False
    changed = False

    def _mutate(current: Any) -> dict[str, Any]:
        nonlocal changed
        data = current if isinstance(current, dict) else {}
        group_data = data.get(gid)
        if isinstance(group_data, dict) and uid in group_data:
            group_data.pop(uid, None)
            changed = True
            if not group_data:
                data.pop(gid, None)
        return data

    get_data_store().mutate_sync(_NS_GROUP_MEMBER_ALIASES, _mutate)
    return changed


def merge_known_names(*items: Any) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        values = item if isinstance(item, (list, tuple, set)) else [item]
        for raw in values:
            name = re.sub(r"\s+", " ", str(raw or "").strip())
            if not name:
                continue
            key = name.casefold()
            if key in seen:
                continue
            seen.add(key)
            names.append(name[:_MAX_ALIAS_CHARS])
    return names


def render_group_alias_context(
    group_id: str,
    *,
    user_id: str = "",
    known_names: dict[str, str | list[str]] | None = None,
    limit: int = 30,
) -> str:
    entries = list_group_member_aliases(group_id)
    if not entries:
        return ""
    current_uid = _norm_id(user_id)
    known = known_names or {}

    def _sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, float, str]:
        uid, entry = item
        return (0 if current_uid and uid == current_uid else 1, -float(entry.get("updated_at", 0) or 0), uid)

    lines = [
        "## 群成员称呼映射",
        "这些是管理员确认的群内外号/称呼。看到这些称呼时，先按对应 QQ 用户理解，再结合上下文决定是否接话。",
        "回复当前说话人、提到某个群员或自然插话时，可以优先使用对应群内称呼；不确定对象或会显得突兀时，就不要强行点名。",
    ]
    for uid, entry in sorted(entries.items(), key=_sort_key)[: max(1, int(limit or 1))]:
        aliases = list(entry.get("aliases") or [])
        if not aliases:
            continue
        label_values = known.get(uid, [])
        label_names = merge_known_names(label_values)
        label = f"（{' / '.join(label_names[:2])}）" if label_names else ""
        prefix = "当前说话人：" if current_uid and uid == current_uid else "- "
        note = str(entry.get("note", "") or "").strip()
        line = f"{prefix}QQ {uid}{label} = {' / '.join(aliases[:_MAX_ALIAS_COUNT])}"
        if note:
            line += f"；备注：{note}"
        lines.append(line)
    return "\n".join(lines) if len(lines) > 2 else ""


__all__ = [
    "delete_group_member_aliases",
    "get_group_member_alias_entry",
    "list_group_member_aliases",
    "merge_known_names",
    "normalize_aliases",
    "render_group_alias_context",
    "set_group_member_aliases",
]
