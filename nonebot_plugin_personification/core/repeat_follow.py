from __future__ import annotations

import re
import threading
import time
from typing import Any, Awaitable, Callable


_REPEAT_FOLLOW_TTL_SECONDS = 5 * 60
_REPEAT_MAX_CHARS = 24
_RECENT_REPEAT_FOLLOW: dict[str, float] = {}
_RECENT_REPEAT_FOLLOW_LOCK = threading.RLock()
_COMMAND_RE = re.compile(r"^\s*[/!！.#＃]")
_LINK_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)
_PURE_NUMBER_RE = re.compile(r"^\d+$")


def _normalize_repeat_key(text: str) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").strip().lower())
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", "", normalized)
    return normalized[:120]


def _cleanup_recent_repeat_cache(now_ts: float | None = None) -> None:
    with _RECENT_REPEAT_FOLLOW_LOCK:
        now_value = float(now_ts or time.time())
        expired = [
            key
            for key, ts in _RECENT_REPEAT_FOLLOW.items()
            if now_value - float(ts or 0.0) >= _REPEAT_FOLLOW_TTL_SECONDS
        ]
        for key in expired:
            _RECENT_REPEAT_FOLLOW.pop(key, None)


def _strongest_repeat_cluster(repeat_clusters: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_score: tuple[int, int, int] | None = None
    for cluster in list(repeat_clusters or []):
        text = str(cluster.get("text", "") or "").strip()
        count = int(cluster.get("count", 0) or 0)
        speaker_count = len(cluster.get("speakers") or [])
        if not text or count <= 0:
            continue
        score = (count, speaker_count, -len(text))
        if best is None or score > (best_score or (0, 0, 0)):
            best = cluster
            best_score = score
    return best


def _is_repeat_text_allowed(text: str) -> bool:
    plain = str(text or "").strip()
    if not plain:
        return False
    if len(plain) > _REPEAT_MAX_CHARS:
        return False
    if "\n" in plain or "\r" in plain:
        return False
    if _COMMAND_RE.search(plain):
        return False
    if _LINK_RE.search(plain):
        return False
    if _PURE_NUMBER_RE.fullmatch(plain):
        return False
    return True


def _repeat_follow_cache_key(group_id: str, text: str) -> str:
    return f"{str(group_id or '').strip()}::{_normalize_repeat_key(text)}"


def _can_follow_repeat(group_id: str, text: str, *, now_ts: float | None = None) -> bool:
    with _RECENT_REPEAT_FOLLOW_LOCK:
        _cleanup_recent_repeat_cache(now_ts)
        key = _repeat_follow_cache_key(group_id, text)
        last_ts = float(_RECENT_REPEAT_FOLLOW.get(key, 0.0) or 0.0)
        now_value = float(now_ts or time.time())
        return (now_value - last_ts) >= _REPEAT_FOLLOW_TTL_SECONDS


def _mark_repeat_follow(group_id: str, text: str, *, now_ts: float | None = None) -> None:
    with _RECENT_REPEAT_FOLLOW_LOCK:
        _cleanup_recent_repeat_cache(now_ts)
        _RECENT_REPEAT_FOLLOW[_repeat_follow_cache_key(group_id, text)] = float(now_ts or time.time())


def _build_repeat_fallback_text(text: str) -> str:
    plain = str(text or "").strip()
    if len(plain) <= _REPEAT_MAX_CHARS:
        return plain
    return plain[:_REPEAT_MAX_CHARS].rstrip()


def _looks_like_existing_repeat(reply_text: str, repeat_text: str) -> bool:
    lhs = _normalize_repeat_key(reply_text)
    rhs = _normalize_repeat_key(repeat_text)
    return bool(lhs and rhs and (lhs == rhs or rhs in lhs))


async def maybe_follow_repeat_cluster(
    *,
    reply_text: str,
    repeat_clusters: list[dict[str, Any]] | None,
    group_id: str,
    raw_message_text: str,
    message_intent: str,
    is_private_session: bool,
    is_random_chat: bool,
    is_direct_mention: bool,
    has_newer_batch: bool = False,
    rewrite_reply: Callable[[str, str], Awaitable[str]] | None = None,
) -> tuple[str, bool]:
    original = str(reply_text or "").strip()
    if not original:
        return original, False
    if is_private_session or is_direct_mention:
        return original, False
    if message_intent != "banter":
        return original, False
    if is_random_chat and has_newer_batch:
        return original, False

    cluster = _strongest_repeat_cluster(repeat_clusters)
    if not cluster:
        return original, False
    cluster_text = str(cluster.get("text", "") or "").strip()
    if not _is_repeat_text_allowed(cluster_text):
        return original, False
    if not _can_follow_repeat(group_id, cluster_text):
        return original, False
    if _looks_like_existing_repeat(original, cluster_text):
        _mark_repeat_follow(group_id, cluster_text)
        return original, True

    candidate = ""
    if rewrite_reply is not None:
        try:
            regenerated = await rewrite_reply(cluster_text, original)
        except Exception:
            regenerated = ""
        candidate = str(regenerated or "").strip()
        if candidate:
            if len(candidate) > _REPEAT_MAX_CHARS:
                candidate = ""
            elif not _is_repeat_text_allowed(candidate):
                candidate = ""

    if not candidate:
        candidate = _build_repeat_fallback_text(cluster_text)
    if not candidate:
        return original, False

    _mark_repeat_follow(group_id, cluster_text)
    return candidate, True


__all__ = ["maybe_follow_repeat_cluster"]
