from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from .data_store import get_data_store


_NS_VERIFY_CODES = "webui_verify_codes"
_NS_DEVICES = "webui_devices"
_NS_RATE_LIMIT = "webui_rate_limit"

_VERIFY_TTL_SECONDS = 300
_VERIFY_MAX_ATTEMPTS = 5  # 单个验证码最多输错 5 次后强制废弃，防止暴力枚举 6 位空间
_RATE_WINDOW_SECONDS = 3600
_RATE_MAX_ATTEMPTS = 5
# 设备 token 7 天过期；每次请求会刷新 last_seen 但不延长到期点（严格 7 天滚动）
_DEVICE_TOKEN_TTL_SECONDS = 7 * 24 * 3600


def _now() -> float:
    return time.time()


def _hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _hash_ip(ip: str) -> str:
    return hashlib.sha256(str(ip or "").encode("utf-8")).hexdigest()[:16]


def create_verify_code(qq: str) -> str:
    """生成 6 位数字验证码，写 KV，返回明文码（仅本次推送用）。
    覆盖该 QQ 已有的验证码（重发即作废旧码）。
    """
    qq_key = str(qq or "").strip()
    if not qq_key:
        raise ValueError("qq required")
    code = f"{secrets.randbelow(1_000_000):06d}"

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data[qq_key] = {
            "code": code,
            "expires_at": _now() + _VERIFY_TTL_SECONDS,
            "fail_count": 0,
        }
        return _prune_expired_codes(data)

    get_data_store().mutate_sync(_NS_VERIFY_CODES, _mutate)
    return code


def consume_verify_code(qq: str, code: str) -> bool:
    """校验验证码。
    成功 → 销毁并返 True；
    失败 → fail_count+1；累计 5 次或验证码本身过期则直接废弃，下次必须重新发送。
    """
    qq_key = str(qq or "").strip()
    target = str(code or "").strip()
    if not qq_key or not target:
        return False
    matched = False

    def _mutate(current: object) -> dict[str, Any]:
        nonlocal matched
        data = current if isinstance(current, dict) else {}
        entry = data.get(qq_key)
        if not isinstance(entry, dict):
            return _prune_expired_codes(data)
        # 过期直接废弃
        if float(entry.get("expires_at", 0)) <= _now():
            data.pop(qq_key, None)
            return _prune_expired_codes(data)
        if str(entry.get("code", "")) == target:
            matched = True
            data.pop(qq_key, None)
        else:
            entry["fail_count"] = int(entry.get("fail_count", 0)) + 1
            if entry["fail_count"] >= _VERIFY_MAX_ATTEMPTS:
                # 超过最大尝试次数，废弃验证码
                data.pop(qq_key, None)
            else:
                data[qq_key] = entry
        return _prune_expired_codes(data)

    get_data_store().mutate_sync(_NS_VERIFY_CODES, _mutate)
    return matched


def _prune_expired_codes(data: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    return {
        qq: entry
        for qq, entry in data.items()
        if isinstance(entry, dict) and float(entry.get("expires_at", 0)) > now
    }


def issue_device_token(qq: str, ua: str, ip: str, label: str = "") -> str:
    """生成 device token + CSRF token，写 KV，返回明文 token（设到 cookie）。"""
    qq_key = str(qq or "").strip()
    if not qq_key:
        raise ValueError("qq required")
    token = secrets.token_urlsafe(32)
    token_hash = _hash_token(token)
    now_ts = _now()
    record = {
        "qq": qq_key,
        "ua": str(ua or "")[:512],
        "ip_hash": _hash_ip(ip),
        "label": str(label or "").strip()[:64] or "未命名设备",
        "created_at": now_ts,
        "last_seen": now_ts,
        "expires_at": now_ts + _DEVICE_TOKEN_TTL_SECONDS,
        "csrf_token": secrets.token_urlsafe(24),
    }

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data[token_hash] = record
        return _prune_expired_devices(data)

    get_data_store().mutate_sync(_NS_DEVICES, _mutate)
    return token


def lookup_device(token: str, *, ua: str = "") -> dict[str, Any] | None:
    """根据 cookie token 查设备记录；命中后刷新 last_seen。
    过期设备视为不存在并被清理。
    """
    target = str(token or "").strip()
    if not target:
        return None
    token_hash = _hash_token(target)
    matched: dict[str, Any] | None = None

    def _mutate(current: object) -> dict[str, Any]:
        nonlocal matched
        data = current if isinstance(current, dict) else {}
        entry = data.get(token_hash)
        if isinstance(entry, dict):
            expires_at = float(entry.get("expires_at", 0) or 0)
            if expires_at > 0 and expires_at <= _now():
                data.pop(token_hash, None)
                return _prune_expired_devices(data)
            stored_ua = str(entry.get("ua", "") or "")
            if ua and stored_ua and stored_ua != str(ua or "")[:512]:
                # UA 不一致：怀疑 cookie 被换设备使用，拒绝
                return data
            entry["last_seen"] = _now()
            matched = dict(entry)
            data[token_hash] = entry
        return _prune_expired_devices(data)

    get_data_store().mutate_sync(_NS_DEVICES, _mutate)
    return matched


def list_devices(qq: str | None = None) -> list[dict[str, Any]]:
    qq_key = str(qq or "").strip()
    data = get_data_store().load_sync(_NS_DEVICES)
    if not isinstance(data, dict):
        return []
    now_ts = _now()
    out: list[dict[str, Any]] = []
    for token_hash, entry in data.items():
        if not isinstance(entry, dict):
            continue
        expires_at = float(entry.get("expires_at", 0) or 0)
        if expires_at > 0 and expires_at <= now_ts:
            continue
        if qq_key and entry.get("qq") != qq_key:
            continue
        item = dict(entry)
        item["id"] = token_hash
        # 不向 API 暴露 csrf_token；调用方需要时单独走 lookup_device
        item.pop("csrf_token", None)
        out.append(item)
    out.sort(key=lambda x: float(x.get("last_seen", 0) or 0), reverse=True)
    return out


def _prune_expired_devices(data: dict[str, Any]) -> dict[str, Any]:
    now_ts = _now()
    return {
        token_hash: entry
        for token_hash, entry in data.items()
        if isinstance(entry, dict)
        and float(entry.get("expires_at", 0) or 0) > now_ts
    }


def prune_expired_devices() -> int:
    """启动时调用：清理已过期的 device token；返回清理数量。"""
    pruned = [0]

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        before = len(data)
        cleaned = _prune_expired_devices(data)
        pruned[0] = before - len(cleaned)
        return cleaned

    get_data_store().mutate_sync(_NS_DEVICES, _mutate)
    return pruned[0]


def revoke_device(device_id: str) -> bool:
    target = str(device_id or "").strip()
    if not target:
        return False
    removed = False

    def _mutate(current: object) -> dict[str, Any]:
        nonlocal removed
        data = current if isinstance(current, dict) else {}
        if target in data:
            data.pop(target, None)
            removed = True
        return data

    get_data_store().mutate_sync(_NS_DEVICES, _mutate)
    return removed


def record_login_attempt(ip: str) -> int:
    """记录登录尝试，返回当前窗口内的累计计数。"""
    bucket = _hash_ip(ip)
    count = 0

    def _mutate(current: object) -> dict[str, Any]:
        nonlocal count
        data = current if isinstance(current, dict) else {}
        entry = data.get(bucket)
        now = _now()
        if not isinstance(entry, dict) or now - float(entry.get("window_start", 0)) > _RATE_WINDOW_SECONDS:
            entry = {"window_start": now, "count": 0}
        entry["count"] = int(entry.get("count", 0)) + 1
        count = int(entry["count"])
        data[bucket] = entry
        return _prune_expired_rate_buckets(data)

    get_data_store().mutate_sync(_NS_RATE_LIMIT, _mutate)
    return count


def is_login_locked(ip: str) -> bool:
    bucket = _hash_ip(ip)
    data = get_data_store().load_sync(_NS_RATE_LIMIT)
    if not isinstance(data, dict):
        return False
    entry = data.get(bucket)
    if not isinstance(entry, dict):
        return False
    if _now() - float(entry.get("window_start", 0)) > _RATE_WINDOW_SECONDS:
        return False
    return int(entry.get("count", 0)) >= _RATE_MAX_ATTEMPTS


def reset_login_attempts(ip: str) -> None:
    bucket = _hash_ip(ip)

    def _mutate(current: object) -> dict[str, Any]:
        data = current if isinstance(current, dict) else {}
        data.pop(bucket, None)
        return data

    get_data_store().mutate_sync(_NS_RATE_LIMIT, _mutate)


def _prune_expired_rate_buckets(data: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    return {
        bucket: entry
        for bucket, entry in data.items()
        if isinstance(entry, dict)
        and now - float(entry.get("window_start", 0)) <= _RATE_WINDOW_SECONDS
    }


__all__ = [
    "create_verify_code",
    "consume_verify_code",
    "issue_device_token",
    "lookup_device",
    "list_devices",
    "revoke_device",
    "prune_expired_devices",
    "record_login_attempt",
    "is_login_locked",
    "reset_login_attempts",
]
