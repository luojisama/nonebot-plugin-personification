from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from typing import Any, Iterator


_LOCK = threading.RLock()
_COUNTERS: dict[str, int] = defaultdict(int)
_TIMINGS: dict[str, dict[str, float]] = defaultdict(
    lambda: {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0}
)


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


def record_counter(name: str, amount: int = 1, **labels: Any) -> None:
    key = _metric_key(name, labels)
    with _LOCK:
        _COUNTERS[key] += int(amount or 0)


def record_timing(name: str, duration_ms: float, **labels: Any) -> None:
    key = _metric_key(name, labels)
    value = max(0.0, float(duration_ms or 0.0))
    with _LOCK:
        bucket = _TIMINGS[key]
        bucket["count"] += 1.0
        bucket["total_ms"] += value
        bucket["max_ms"] = max(bucket["max_ms"], value)


def snapshot_metrics() -> dict[str, Any]:
    with _LOCK:
        counters = [{"name": key, "value": int(value)} for key, value in _COUNTERS.items()]
        timings = []
        for key, bucket in _TIMINGS.items():
            count = float(bucket.get("count", 0.0) or 0.0)
            total_ms = float(bucket.get("total_ms", 0.0) or 0.0)
            timings.append(
                {
                    "name": key,
                    "count": int(count),
                    "total_ms": round(total_ms, 2),
                    "avg_ms": round(total_ms / count, 2) if count > 0 else 0.0,
                    "max_ms": round(float(bucket.get("max_ms", 0.0) or 0.0), 2),
                }
            )
    counters.sort(key=lambda item: (-int(item["value"]), str(item["name"])))
    timings.sort(key=lambda item: (-float(item["total_ms"]), str(item["name"])))
    return {"counters": counters, "timings": timings}


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
    with _LOCK:
        _COUNTERS.clear()
        _TIMINGS.clear()


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
