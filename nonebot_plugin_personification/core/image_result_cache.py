from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


_CACHE_LOCK = threading.Lock()
_CACHE_STATE: dict[str, dict[str, Any]] | None = None
_MAX_CACHE_ITEMS = 1024
_REFRESH_HINTS = (
    "刷新",
    "重新识别",
    "重新翻译",
    "重新分析",
    "重识别",
    "重翻",
    "再识别",
    "再翻译",
    "重新看",
    "refresh",
    "retry",
    "recheck",
)


def _data_dir() -> Path:
    # 第一优先级：读取 NoneBot 配置中的 personification_data_dir
    # 与 inner_state.py 的 get_personification_data_dir() 逻辑保持一致，
    # 确保所有数据文件使用同一个固定目录，不随 bot 账号变化。
    try:
        from nonebot import get_driver

        configured = str(
            getattr(get_driver().config, "personification_data_dir", "") or ""
        ).strip()
        if configured:
            return Path(configured)
    except Exception:
        pass

    # 第二优先级：nonebot_plugin_localstore 标准插件数据目录
    try:
        import nonebot_plugin_localstore as store

        return Path(store.get_plugin_data_dir())
    except Exception:
        pass

    # 兜底：相对路径
    return Path("data") / "personification"


def _cache_path() -> Path:
    return _data_dir() / "image_result_cache.json"


def has_refresh_hint(text: str) -> bool:
    query = str(text or "").strip().lower()
    if not query:
        return False
    return any(token in query for token in _REFRESH_HINTS)


def normalize_cache_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return ""
    for token in _REFRESH_HINTS:
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def image_fingerprint(image_url: str) -> str:
    raw = str(image_url or "").strip()
    if raw.startswith("data:") and "," in raw:
        raw = raw.split(",", 1)[1]
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_image_cache_key(image_url: str, payload: dict[str, Any]) -> str:
    normalized_payload = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    seed = f"{image_fingerprint(image_url)}\n{normalized_payload}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _load_cache_unlocked() -> dict[str, dict[str, Any]]:
    global _CACHE_STATE
    if _CACHE_STATE is not None:
        return _CACHE_STATE

    path = _cache_path()
    if not path.exists():
        _CACHE_STATE = {}
        return _CACHE_STATE

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    _CACHE_STATE = raw if isinstance(raw, dict) else {}
    return _CACHE_STATE


def _persist_cache_unlocked(cache: dict[str, dict[str, Any]]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prune_cache_unlocked(cache: dict[str, dict[str, Any]]) -> None:
    while len(cache) > _MAX_CACHE_ITEMS:
        cache.pop(next(iter(cache)), None)


async def get_cached_image_result(cache_key: str) -> str | None:
    def _get() -> str | None:
        with _CACHE_LOCK:
            cache = _load_cache_unlocked()
            entry = cache.get(str(cache_key or "").strip())
            if not isinstance(entry, dict):
                return None
            result = entry.get("result")
            if isinstance(result, str) and result.strip():
                return result.strip()
            return None

    return await asyncio.to_thread(_get)


async def set_cached_image_result(
    cache_key: str,
    result: str,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    value = str(result or "").strip()
    if not value:
        return

    def _set() -> None:
        with _CACHE_LOCK:
            cache = _load_cache_unlocked()
            cache[str(cache_key or "").strip()] = {
                "result": value,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "meta": meta or {},
            }
            _prune_cache_unlocked(cache)
            _persist_cache_unlocked(cache)

    await asyncio.to_thread(_set)


async def clear_image_result_cache() -> int:
    def _clear() -> int:
        global _CACHE_STATE
        with _CACHE_LOCK:
            cache = _load_cache_unlocked()
            count = len(cache)
            cache.clear()
            _CACHE_STATE = {}
            path = _cache_path()
            if path.exists():
                try:
                    path.unlink()
                except Exception:
                    _persist_cache_unlocked(_CACHE_STATE)
            return count

    return await asyncio.to_thread(_clear)
