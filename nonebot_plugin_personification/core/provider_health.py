"""Provider 健康统计 + 动态优先级计算。

数据流：
- `_call_provider_once` 每次真实请求 finally 调 `record_request_result`，
  把 (latency_ms, success, error_kind) 累计到 SQLite provider_health_stats 表。
- avg_latency_ms 用 EMA（α=0.3）平滑，避免单次极端值剧烈影响排序。
- `get_provider_candidates` 排序时调 `compute_effective_priority`：
    effective = base_priority + latency_penalty + failure_penalty
  样本数 < min_samples 时直接返回 base_priority，避免冷启动 fluke。

被动统计 vs 探活（后续子批 2）：
- 真实请求 stats 为主（覆盖真实负载场景）
- 探活只是补样本 + 触发 cooldown 解除，不另存表
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .db import connect_sync

# EMA 平滑系数：α=0.3 让新样本权重 30%，老平均权重 70%
_EMA_ALPHA = 0.3

# stats 写入并发：sqlite WAL 已经足够，但加进程内锁防止
# record_request_result 在不同 asyncio worker 同时跑时引发 OperationalError
_STATS_LOCK = threading.Lock()

# 错误分类：仅用于诊断，不影响排序
_ERROR_KINDS = {"timeout", "rate_limit", "5xx", "4xx", "connect", "vision_unavailable", "other"}


def classify_error(exc: Exception | str) -> str:
    """把异常归类成有限的 kind 标签。"""
    if exc is None:
        return ""
    text = f"{type(exc).__name__} {exc}".lower()
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "connect" in text or "tls" in text:
        return "connect"
    if "429" in text or "rate limit" in text or "too many requests" in text:
        return "rate_limit"
    # 走 httpx HTTPStatusError 的 response.status_code 优先
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    try:
        code = int(status or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 429:
        return "rate_limit"
    if 500 <= code < 600:
        return "5xx"
    if 400 <= code < 500:
        return "4xx"
    return "other"


def record_request_result(
    *,
    provider_name: str,
    latency_ms: float,
    success: bool,
    error_kind: str = "",
) -> None:
    """记一次真实请求结果到 SQLite。失败安全（异常吞掉不影响主流程）。"""
    name = str(provider_name or "").strip()
    if not name:
        return
    lat = max(0.0, float(latency_ms or 0))
    now = time.time()
    try:
        with _STATS_LOCK, connect_sync() as conn:
            row = conn.execute(
                """
                SELECT sample_count, success_count, failure_count, avg_latency_ms
                FROM provider_health_stats
                WHERE provider_name = ?
                """,
                (name,),
            ).fetchone()
            if row is None:
                # 首次：直接落库（不用 EMA，让首样本就是平均）
                conn.execute(
                    """
                    INSERT INTO provider_health_stats(
                        provider_name, sample_count, success_count, failure_count,
                        avg_latency_ms, last_request_at, last_success_at, last_failure_at,
                        last_error_kind, last_seen_at
                    ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        name,
                        1 if success else 0,
                        0 if success else 1,
                        lat,
                        now,
                        now if success else None,
                        None if success else now,
                        str(error_kind or "")[:32],
                        now,
                    ),
                )
            else:
                sample_count = int(row["sample_count"] or 0) + 1
                success_count = int(row["success_count"] or 0) + (1 if success else 0)
                failure_count = int(row["failure_count"] or 0) + (0 if success else 1)
                # EMA：avg = α·new + (1-α)·old；老值为 0 时把 new 当起点
                old_avg = float(row["avg_latency_ms"] or 0)
                new_avg = lat if old_avg <= 0 else _EMA_ALPHA * lat + (1 - _EMA_ALPHA) * old_avg
                conn.execute(
                    """
                    UPDATE provider_health_stats SET
                        sample_count = ?,
                        success_count = ?,
                        failure_count = ?,
                        avg_latency_ms = ?,
                        last_request_at = ?,
                        last_success_at = COALESCE(?, last_success_at),
                        last_failure_at = COALESCE(?, last_failure_at),
                        last_error_kind = ?,
                        last_seen_at = ?
                    WHERE provider_name = ?
                    """,
                    (
                        sample_count,
                        success_count,
                        failure_count,
                        new_avg,
                        now,
                        now if success else None,
                        None if success else now,
                        str(error_kind or "")[:32],
                        now,
                        name,
                    ),
                )
            conn.commit()
    except Exception:
        # provider 调用本身已经够脆弱，stats 写失败绝不影响主流程
        return


def get_stats(provider_name: str) -> dict[str, Any] | None:
    name = str(provider_name or "").strip()
    if not name:
        return None
    try:
        with connect_sync() as conn:
            row = conn.execute(
                """
                SELECT provider_name, sample_count, success_count, failure_count,
                       avg_latency_ms, last_request_at, last_success_at, last_failure_at,
                       last_error_kind, last_seen_at
                FROM provider_health_stats
                WHERE provider_name = ?
                """,
                (name,),
            ).fetchone()
    except Exception:
        return None
    return _row_to_dict(row)


def get_all_stats() -> dict[str, dict[str, Any]]:
    try:
        with connect_sync() as conn:
            rows = conn.execute(
                """
                SELECT provider_name, sample_count, success_count, failure_count,
                       avg_latency_ms, last_request_at, last_success_at, last_failure_at,
                       last_error_kind, last_seen_at
                FROM provider_health_stats
                """
            ).fetchall()
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        d = _row_to_dict(row)
        if d:
            out[d["provider_name"]] = d
    return out


def _row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "provider_name": str(row["provider_name"] or ""),
        "sample_count": int(row["sample_count"] or 0),
        "success_count": int(row["success_count"] or 0),
        "failure_count": int(row["failure_count"] or 0),
        "avg_latency_ms": float(row["avg_latency_ms"] or 0),
        "last_request_at": float(row["last_request_at"] or 0),
        "last_success_at": float(row["last_success_at"] or 0),
        "last_failure_at": float(row["last_failure_at"] or 0),
        "last_error_kind": str(row["last_error_kind"] or ""),
        "last_seen_at": float(row["last_seen_at"] or 0),
    }


def reset_stats(provider_name: str) -> bool:
    name = str(provider_name or "").strip()
    if not name:
        return False
    try:
        with _STATS_LOCK, connect_sync() as conn:
            cur = conn.execute(
                "DELETE FROM provider_health_stats WHERE provider_name = ?", (name,)
            )
            conn.commit()
            return int(cur.rowcount or 0) > 0
    except Exception:
        return False


def prune_old_stats(*, max_age_days: int = 30) -> int:
    """删除 max_age_days 天没出现的 provider；返回删除条数。"""
    cutoff = time.time() - max(1, int(max_age_days or 30)) * 86400
    try:
        with _STATS_LOCK, connect_sync() as conn:
            cur = conn.execute(
                "DELETE FROM provider_health_stats WHERE last_seen_at < ?",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
    except Exception:
        return 0


def compute_effective_priority(
    provider: dict[str, Any],
    stats: dict[str, Any] | None,
    *,
    min_samples: int = 3,
    enabled: bool = True,
) -> float:
    """根据 stats 计算 provider 的 effective_priority（float，越大越往后排）。

    样本数 < min_samples 或 enabled=False 时直接返回 base，避免冷启动 fluke。
    """
    try:
        base = float(provider.get("priority", 0) or 0)
    except (TypeError, ValueError):
        base = 0.0
    if not enabled:
        return base
    if not stats:
        return base
    sample_count = int(stats.get("sample_count", 0) or 0)
    if sample_count < max(1, int(min_samples or 3)):
        return base
    avg_latency = float(stats.get("avg_latency_ms", 0) or 0)
    success_count = int(stats.get("success_count", 0) or 0)
    success_rate = success_count / sample_count if sample_count > 0 else 1.0
    # latency 惩罚：每超过 1 秒 +0.001*ms 大约相当于 2 秒 → +2，5 秒 → +8
    latency_penalty = max(0.0, (avg_latency - 1000.0) / 500.0)
    # 失败率惩罚：100% 失败 +10，50% 失败 +5。
    # 额外规则：样本足够但从未成功过的 provider 不能因为"失败得快"
    # 排在稳定成功但较慢的 provider 前面。
    failure_penalty = (1.0 - success_rate) * 10.0
    if success_count <= 0:
        failure_penalty += 100.0
    return base + latency_penalty + failure_penalty


def compute_cooldown_seconds(failures: int, *, is_rate_limit: bool = False, retry_after: float = 0.0) -> float:
    """指数退避 + ±20% jitter。429 单独走更长的基础 cooldown。

    failures=1 → ~60s，failures=2 → ~120s，failures=3 → ~240s，
    失败 5 次 → ~960s（封顶 1800s = 30 分钟）。
    """
    import random

    n = max(1, int(failures or 1))
    if is_rate_limit:
        # 429：默认 600s 起步，若服务端给了 retry_after 取较大者，封顶 30 分钟
        base = max(600.0, float(retry_after or 0))
        base = min(1800.0, base)
    else:
        base = min(1800.0, 60.0 * (2 ** (n - 1)))
    jitter = base * 0.2  # ±20%
    delta = random.uniform(-jitter, jitter)
    cooldown = max(15.0, base + delta)
    if is_rate_limit and retry_after:
        cooldown = max(cooldown, min(1800.0, float(retry_after or 0)))
    return cooldown


__all__ = [
    "classify_error",
    "record_request_result",
    "get_stats",
    "get_all_stats",
    "reset_stats",
    "prune_old_stats",
    "compute_effective_priority",
    "compute_cooldown_seconds",
]
