from __future__ import annotations

import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from math import ceil
from typing import Any, Iterator


_LOCK = threading.RLock()
_MAX_SERIES = 256
_MAX_TIMING_SAMPLES = 128
_COUNTER_OVERFLOW_KEY = "metrics_overflow{kind=counter}"
_TIMING_OVERFLOW_KEY = "metrics_overflow{kind=timing}"
_COUNTERS: dict[str, int] = defaultdict(int)
_TIMINGS: dict[str, dict[str, Any]] = {}
_OVERFLOW_TOTAL = 0


def _metric_key(name: str, labels: dict[str, Any] | None = None) -> str:
    metric_name = str(name or "").strip() or "unnamed_metric"
    payload = dict(labels or {})
    if not payload:
        return metric_name
    suffix = ",".join(
        f"{str(key).strip()}={str(payload[key]).strip()}"
        for key in sorted(payload)
        if str(key).strip()
    )
    return f"{metric_name}{{{suffix}}}" if suffix else metric_name


def _bounded_key(key: str, *, kind: str) -> str:
    global _OVERFLOW_TOTAL
    own = _COUNTERS if kind == "counter" else _TIMINGS
    if key in own:
        return key
    # Reserve one overflow bucket for each metric kind so the total never
    # exceeds the documented process-wide limit.
    regular_limit = _MAX_SERIES - 2
    if len(_COUNTERS) + len(_TIMINGS) < regular_limit:
        return key
    _OVERFLOW_TOTAL += 1
    return _COUNTER_OVERFLOW_KEY if kind == "counter" else _TIMING_OVERFLOW_KEY


def _new_timing_bucket() -> dict[str, Any]:
    return {
        "count": 0.0,
        "total_ms": 0.0,
        "max_ms": 0.0,
        "samples": deque(maxlen=_MAX_TIMING_SAMPLES),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def record_counter(name: str, amount: int = 1, **labels: Any) -> None:
    with _LOCK:
        key = _bounded_key(_metric_key(name, labels), kind="counter")
        _COUNTERS[key] += int(amount or 0)


def record_timing(name: str, duration_ms: float, **labels: Any) -> None:
    value = max(0.0, float(duration_ms or 0.0))
    with _LOCK:
        key = _bounded_key(_metric_key(name, labels), kind="timing")
        bucket = _TIMINGS.setdefault(key, _new_timing_bucket())
        bucket["count"] += 1.0
        bucket["total_ms"] += value
        bucket["max_ms"] = max(bucket["max_ms"], value)
        bucket["samples"].append(value)


def snapshot_metrics() -> dict[str, Any]:
    with _LOCK:
        counters = [{"name": key, "value": int(value)} for key, value in _COUNTERS.items()]
        timings = []
        for key, bucket in _TIMINGS.items():
            count = float(bucket.get("count", 0.0) or 0.0)
            total_ms = float(bucket.get("total_ms", 0.0) or 0.0)
            samples = [float(value) for value in bucket.get("samples", ())]
            timings.append(
                {
                    "name": key,
                    "count": int(count),
                    "total_ms": round(total_ms, 2),
                    "avg_ms": round(total_ms / count, 2) if count > 0 else 0.0,
                    "max_ms": round(float(bucket.get("max_ms", 0.0) or 0.0), 2),
                    "recent_count": len(samples),
                    "recent_max_ms": round(max(samples), 2) if samples else 0.0,
                    "p50_ms": round(_percentile(samples, 0.50), 2),
                    "p95_ms": round(_percentile(samples, 0.95), 2),
                }
            )
        series = {
            "limit": _MAX_SERIES,
            "used": len(_COUNTERS) + len(_TIMINGS),
            "overflow_total": _OVERFLOW_TOTAL,
        }
    counters.sort(key=lambda item: (-int(item["value"]), str(item["name"])))
    timings.sort(key=lambda item: (-float(item["total_ms"]), str(item["name"])))
    return {"counters": counters, "timings": timings, "series": series}


def format_metrics_snapshot(*, top_n: int = 8) -> str:
    snapshot = snapshot_metrics()
    counter_lines = [
        f"- {item['name']}: {item['value']}"
        for item in list(snapshot["counters"])[: max(1, int(top_n or 1))]
    ]
    timing_lines = [
        f"- {item['name']}: count={item['count']} avg={item['avg_ms']}ms max={item['max_ms']}ms"
        for item in list(snapshot["timings"])[: max(1, int(top_n or 1))]
    ]
    lines = ["运行时指标"]
    lines.append("计数器：" if counter_lines else "计数器：暂无数据")
    lines.extend(counter_lines)
    lines.append("耗时：" if timing_lines else "耗时：暂无数据")
    lines.extend(timing_lines)
    return "\n".join(lines)


def reset_metrics() -> None:
    global _OVERFLOW_TOTAL
    with _LOCK:
        _COUNTERS.clear()
        _TIMINGS.clear()
        _OVERFLOW_TOTAL = 0


@contextmanager
def timed_metric(name: str, **labels: Any) -> Iterator[None]:
    import time

    started_at = time.monotonic()
    try:
        yield
    finally:
        record_timing(name, (time.monotonic() - started_at) * 1000.0, **labels)


__all__ = [
    "format_metrics_snapshot",
    "record_counter",
    "record_timing",
    "reset_metrics",
    "snapshot_metrics",
    "timed_metric",
]
