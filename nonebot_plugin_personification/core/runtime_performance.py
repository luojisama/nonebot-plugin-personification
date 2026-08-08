from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from math import ceil
from pathlib import Path
from typing import Any

from . import metrics, plugin_runtime_logs
from .runtime_task_supervisor import runtime_task_supervisor


_STARTED_AT = time.time()
_LAG_SAMPLES: deque[float] = deque(maxlen=300)
_LAG_LOCK = threading.RLock()
_REPLY_REPORTER: Callable[[], dict[str, Any]] | None = None
_CACHE_REPORTERS: dict[str, Callable[[], dict[str, Any]]] = {}
_REPORTER_LOCK = threading.RLock()


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def _linux_memory() -> tuple[int | None, int | None]:
    if not sys.platform.startswith("linux"):
        return None, None
    rss = None
    try:
        fields = Path("/proc/self/statm").read_text(encoding="ascii").strip().split()
        if len(fields) >= 2:
            rss = int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    except (OSError, ValueError, TypeError):
        rss = None
    peak = None
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    except (ImportError, OSError, ValueError, TypeError):
        peak = None
    return rss, peak


def _windows_memory() -> tuple[int | None, int | None]:
    if os.name != "nt":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCountersEx(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCountersEx()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        if not ok:
            return None, None
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    except (AttributeError, OSError, TypeError, ValueError):
        return None, None


def _process_memory() -> tuple[int | None, int | None]:
    if os.name == "nt":
        return _windows_memory()
    return _linux_memory()


def _fd_count() -> int | None:
    if not sys.platform.startswith("linux"):
        return None
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def process_snapshot() -> dict[str, Any]:
    rss, peak = _process_memory()
    try:
        gc_stats = [
            {
                "collections": int(item.get("collections", 0) or 0),
                "collected": int(item.get("collected", 0) or 0),
                "uncollectable": int(item.get("uncollectable", 0) or 0),
            }
            for item in gc.get_stats()
        ]
    except (AttributeError, TypeError, ValueError):
        gc_stats = []
    return {
        "rss_bytes": rss,
        "peak_rss_bytes": peak,
        "threads": threading.active_count(),
        "fd_count": _fd_count(),
        "uptime_seconds": round(max(0.0, time.time() - _STARTED_AT), 3),
        "gc": {
            "counts": [int(value) for value in gc.get_count()],
            "generations": gc_stats,
        },
    }


async def sample_event_loop_lag(*, interval: float = 1.0) -> None:
    delay = max(0.1, float(interval or 1.0))
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await asyncio.sleep(delay)
        lag_ms = max(0.0, (loop.time() - started - delay) * 1000.0)
        with _LAG_LOCK:
            _LAG_SAMPLES.append(lag_ms)


def event_loop_snapshot() -> dict[str, Any]:
    with _LAG_LOCK:
        values = list(_LAG_SAMPLES)
    return {
        "latest_ms": round(values[-1], 2) if values else 0.0,
        "p50_ms": round(_percentile(values, 0.50), 2),
        "p95_ms": round(_percentile(values, 0.95), 2),
        "max_ms": round(max(values), 2) if values else 0.0,
        "samples": len(values),
    }


def register_reply_reporter(reporter: Callable[[], dict[str, Any]] | None) -> None:
    global _REPLY_REPORTER
    with _REPORTER_LOCK:
        _REPLY_REPORTER = reporter


def register_cache_reporter(name: str, reporter: Callable[[], dict[str, Any]]) -> None:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("cache reporter name is required")
    with _REPORTER_LOCK:
        if normalized not in _CACHE_REPORTERS and len(_CACHE_REPORTERS) >= 32:
            raise ValueError("too many cache reporters")
        _CACHE_REPORTERS[normalized] = reporter


def _safe_report(reporter: Callable[[], dict[str, Any]] | None) -> dict[str, Any]:
    if reporter is None:
        return {}
    try:
        value = reporter()
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _reply_snapshot() -> dict[str, int]:
    with _REPORTER_LOCK:
        reporter = _REPLY_REPORTER
    value = _safe_report(reporter)
    return {
        "active": max(0, int(value.get("active", 0) or 0)),
        "waiting": max(0, int(value.get("waiting", 0) or 0)),
        "session_gates": max(0, int(value.get("session_gates", 0) or 0)),
    }


def _cache_snapshots() -> list[dict[str, Any]]:
    metric_series = metrics.snapshot_metrics().get("series", {})
    items = [
        {
            "name": "runtime_metrics_series",
            "entries": int(metric_series.get("used", 0) or 0),
            "limit": int(metric_series.get("limit", 0) or 0),
            "evictions": int(metric_series.get("overflow_total", 0) or 0),
        }
    ]
    with _REPORTER_LOCK:
        reporters = list(sorted(_CACHE_REPORTERS.items()))
    for name, reporter in reporters:
        value = _safe_report(reporter)
        items.append(
            {
                "name": name,
                "entries": max(0, int(value.get("entries", 0) or 0)),
                "limit": max(0, int(value.get("limit", 0) or 0)),
                "evictions": max(0, int(value.get("evictions", 0) or 0)),
            }
        )
    return items


def snapshot() -> dict[str, Any]:
    writer = plugin_runtime_logs.writer_status()
    return {
        "schema_version": 1,
        "sampled_at": time.time(),
        "process": process_snapshot(),
        "event_loop": event_loop_snapshot(),
        "tasks": runtime_task_supervisor.snapshot(),
        "reply": _reply_snapshot(),
        "queues": {
            "runtime_logs": {
                "depth": max(0, int(writer.get("pending", 0) or 0)),
                "capacity": max(0, int(writer.get("capacity", 0) or 0)),
                "dropped": max(0, int(writer.get("dropped", 0) or 0)),
            }
        },
        "caches": _cache_snapshots(),
    }


def reset_for_testing() -> None:
    global _REPLY_REPORTER
    with _LAG_LOCK:
        _LAG_SAMPLES.clear()
    with _REPORTER_LOCK:
        _REPLY_REPORTER = None
        _CACHE_REPORTERS.clear()


__all__ = [
    "event_loop_snapshot",
    "process_snapshot",
    "register_cache_reporter",
    "register_reply_reporter",
    "reset_for_testing",
    "sample_event_loop_lag",
    "snapshot",
]
