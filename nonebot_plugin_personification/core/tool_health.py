from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, asdict
from typing import Any


_PROBE_INTERVAL_HOURS = 5
_PROBE_JOB_ID = "personification_tool_health_probe"
_STARTUP_TASK: asyncio.Task | None = None
_STATUSES: dict[str, "ToolHealthStatus"] = {}
_LOCK = asyncio.Lock()

_SAFE_PROBE_ARGS: dict[str, dict[str, Any]] = {
    "web_search": {"query": "北京 今日 天气"},
    "search_web": {"query": "北京 今日 天气", "limit": 1},
    "wiki_lookup": {"query": "初音未来"},
    "resolve_acg_entity": {"query": "初音未来"},
    "weather": {"city": "北京", "days": 1},
    "search_images": {"query": "初音未来", "limit": 1},
    "collect_resources": {"query": "Python 官方文档", "resource_type": "文档", "max_count": 1},
    "search_official_site": {"query": "Python documentation", "limit": 1},
    "search_github_repos": {"query": "python", "limit": 1},
    "get_daily_news": {},
    "get_ai_news": {},
    "get_trending": {"platform": "微博"},
    "get_history_today": {},
    "get_gold_price": {},
    "get_exchange_rate": {},
}

_UNAVAILABLE_HINTS = (
    "timeout",
    "timed out",
    "connect timeout",
    "read timeout",
    "connection error",
    "network",
    "proxy",
    "dns",
    "无法访问",
    "连接失败",
    "请求失败",
    "查询失败",
    "超时",
    "空结果",
)


@dataclass
class ToolHealthStatus:
    name: str
    available: bool
    last_checked_at: float
    last_error: str = ""
    latency_ms: int = 0
    disabled_reason: str = ""


def _now() -> float:
    return time.time()


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "").strip()


def _is_probe_candidate(tool: Any) -> bool:
    name = _tool_name(tool)
    if not name or name not in _SAFE_PROBE_ARGS:
        return False
    metadata = getattr(tool, "metadata", {}) if isinstance(getattr(tool, "metadata", {}), dict) else {}
    if not bool(metadata.get("requires_network", False)):
        return False
    if str(metadata.get("side_effect", "none") or "none") != "none":
        return False
    try:
        return bool(tool.enabled())
    except Exception:
        return False


def _result_looks_unavailable(result: Any) -> bool:
    if isinstance(result, dict):
        if result.get("error"):
            return True
        if result.get("ok") is False:
            return True
        return not bool(result)
    if isinstance(result, (list, tuple)):
        return not bool(result)
    text = str(result or "").strip()
    if not text:
        return True
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        return _result_looks_unavailable(parsed)
    lowered = text.lower()
    return any(hint in lowered for hint in _UNAVAILABLE_HINTS)


def is_tool_temporarily_disabled(name: str) -> bool:
    status = _STATUSES.get(str(name or "").strip())
    return bool(status and not status.available)


def get_tool_health_statuses() -> list[dict[str, Any]]:
    return [asdict(status) for status in sorted(_STATUSES.values(), key=lambda item: item.name)]


def reset_tool_health_statuses() -> None:
    _STATUSES.clear()


async def probe_tool(tool: Any, *, timeout_seconds: float = 20.0) -> ToolHealthStatus | None:
    name = _tool_name(tool)
    if not _is_probe_candidate(tool):
        return None
    args = dict(_SAFE_PROBE_ARGS.get(name) or {})
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(tool.handler(**args), timeout=max(1.0, float(timeout_seconds or 20.0)))
        latency_ms = int((time.monotonic() - started) * 1000)
        if _result_looks_unavailable(result):
            raise RuntimeError(str(result or "empty result")[:300])
        status = ToolHealthStatus(
            name=name,
            available=True,
            last_checked_at=_now(),
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        status = ToolHealthStatus(
            name=name,
            available=False,
            last_checked_at=_now(),
            last_error=str(exc or "")[:300],
            latency_ms=latency_ms,
            disabled_reason="startup_or_periodic_probe_failed",
        )
    _STATUSES[name] = status
    return status


async def probe_registry_tools(
    *,
    registry: Any,
    logger: Any = None,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    tools = []
    try:
        tools = [tool for tool in registry.all() if _is_probe_candidate(tool)]
    except Exception:
        tools = []
    checked = 0
    disabled = 0
    restored = 0
    for tool in tools:
        previous_disabled = is_tool_temporarily_disabled(_tool_name(tool))
        status = await probe_tool(tool, timeout_seconds=timeout_seconds)
        if status is None:
            continue
        checked += 1
        if status.available and previous_disabled:
            restored += 1
            if logger is not None:
                logger.info(f"[tool_health] 工具恢复可用：{status.name} latency_ms={status.latency_ms}")
        elif not status.available:
            disabled += 1
            if logger is not None:
                logger.warning(f"[tool_health] 工具暂时屏蔽：{status.name} error={status.last_error}")
    return {"checked": checked, "disabled": disabled, "restored": restored}


async def _probe_registry_tools_guarded(*, registry: Any, logger: Any = None, timeout_seconds: float = 20.0) -> None:
    async with _LOCK:
        try:
            result = await probe_registry_tools(
                registry=registry,
                logger=logger,
                timeout_seconds=timeout_seconds,
            )
            if logger is not None:
                logger.info(
                    "[tool_health] 工具巡检完成 "
                    f"checked={result.get('checked', 0)} disabled={result.get('disabled', 0)} restored={result.get('restored', 0)}"
                )
        except Exception as exc:
            if logger is not None:
                logger.warning(f"[tool_health] 工具巡检失败：{exc}")


def schedule_tool_health_probes(
    *,
    registry: Any,
    scheduler: Any = None,
    logger: Any = None,
    startup_delay_seconds: float = 10.0,
    timeout_seconds: float = 20.0,
) -> None:
    global _STARTUP_TASK
    if registry is None:
        return

    async def _startup_probe() -> None:
        await asyncio.sleep(max(0.0, float(startup_delay_seconds or 0.0)))
        await _probe_registry_tools_guarded(registry=registry, logger=logger, timeout_seconds=timeout_seconds)

    try:
        if _STARTUP_TASK is None or _STARTUP_TASK.done():
            _STARTUP_TASK = asyncio.create_task(_startup_probe())
    except RuntimeError:
        pass

    if scheduler is None:
        return

    async def _periodic_probe() -> None:
        await _probe_registry_tools_guarded(registry=registry, logger=logger, timeout_seconds=timeout_seconds)

    try:
        scheduler.add_job(
            _periodic_probe,
            "interval",
            hours=_PROBE_INTERVAL_HOURS,
            id=_PROBE_JOB_ID,
            replace_existing=True,
        )
        if logger is not None:
            logger.info(f"[tool_health] 已注册工具巡检任务，间隔 {_PROBE_INTERVAL_HOURS} 小时")
    except Exception as exc:
        if logger is not None:
            logger.warning(f"[tool_health] 注册工具巡检任务失败：{exc}")


__all__ = [
    "get_tool_health_statuses",
    "is_tool_temporarily_disabled",
    "probe_registry_tools",
    "probe_tool",
    "reset_tool_health_statuses",
    "schedule_tool_health_probes",
]
