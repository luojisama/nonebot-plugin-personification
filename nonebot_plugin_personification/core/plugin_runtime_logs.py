from __future__ import annotations

import json
import re
import time
from typing import Any

from .db import connect_sync


_LEVEL_ORDER = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "EXCEPTION": 40,
    "CRITICAL": 50,
}
_DEFAULT_RETENTION_DAYS = 7
_DEFAULT_MAX_ENTRIES = 10000
_LAST_PRUNE_AT = 0.0

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|authorization|cookie|token|csrf|refresh[_-]?token|access[_-]?token)(\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(sk-[a-z0-9_-]{8})[a-z0-9_-]+"),
)


def _level_value(level: Any) -> int:
    text = str(level or "INFO").strip().upper()
    return _LEVEL_ORDER.get(text, 20)


def _normalize_level(level: Any) -> str:
    text = str(level or "INFO").strip().upper()
    if text == "WARN":
        return "WARNING"
    if text == "EXCEPTION":
        return "ERROR"
    return text if text in _LEVEL_ORDER else "INFO"


def _limit_text(text: Any, limit: int = 4000) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 12)] + "...<truncated>"


def sanitize_text(value: Any) -> str:
    text = _limit_text(value)
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)(bearer"):
            text = pattern.sub(r"\1***", text)
        elif "sk-" in pattern.pattern:
            text = pattern.sub(r"\1***", text)
        else:
            text = pattern.sub(r"\1\2***", text)
    return text


def _sanitize_obj(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return "<nested>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:40]:
            key_text = str(key or "")[:80]
            if re.search(r"(?i)(key|token|secret|cookie|authorization|csrf)", key_text):
                out[key_text] = "***"
            else:
                out[key_text] = _sanitize_obj(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize_obj(item, depth + 1) for item in list(value)[:40]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return sanitize_text(value)


def _config_int(config: Any, name: str, default: int, *, minimum: int = 1) -> int:
    try:
        value = int(getattr(config, name, default) or default)
    except Exception:
        value = default
    return max(minimum, value)


def retention_days_from_config(config: Any) -> int:
    return _config_int(config, "personification_webui_log_retention_days", _DEFAULT_RETENTION_DAYS, minimum=1)


def max_entries_from_config(config: Any) -> int:
    return _config_int(config, "personification_webui_log_max_entries", _DEFAULT_MAX_ENTRIES, minimum=100)


def capture_level_from_config(config: Any) -> str:
    return _normalize_level(getattr(config, "personification_webui_log_capture_level", "INFO"))


def record(
    *,
    level: str,
    message: Any,
    source: str = "",
    context: dict[str, Any] | None = None,
    trace_id: str = "",
    min_level: str = "INFO",
) -> None:
    if _level_value(level) < _level_value(min_level):
        return
    try:
        payload = json.dumps(_sanitize_obj(context or {}), ensure_ascii=False, separators=(",", ":"))
        with connect_sync() as conn:
            conn.execute(
                """
                INSERT INTO plugin_runtime_logs(ts, level, source, message, context, trace_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    _normalize_level(level)[:16],
                    sanitize_text(source)[:96],
                    sanitize_text(message),
                    payload[:4000],
                    str(trace_id or "")[:64],
                ),
            )
            conn.commit()
    except Exception:
        return


def query_recent(
    *,
    limit: int = 200,
    level: str = "",
    q: str = "",
    cursor: int = 0,
    trace_id: str = "",
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: list[Any] = []
    normalized_level = _normalize_level(level) if level else ""
    if normalized_level:
        min_value = _level_value(normalized_level)
        allowed = [name for name, value in _LEVEL_ORDER.items() if value >= min_value and name != "WARN"]
        placeholders = ",".join("?" for _ in allowed)
        clauses.append(f"level IN ({placeholders})")
        params.extend(allowed)
    if q:
        clauses.append("(message LIKE ? OR source LIKE ? OR trace_id LIKE ?)")
        like = f"%{str(q)[:120]}%"
        params.extend([like, like, like])
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(str(trace_id)[:64])
    if cursor > 0:
        clauses.append("id < ?")
        params.append(int(cursor))
    params.append(max(1, min(int(limit or 200), 500)))
    with connect_sync() as conn:
        rows = conn.execute(
            f"""
            SELECT id, ts, level, source, message, context, trace_id
            FROM plugin_runtime_logs
            WHERE {' AND '.join(clauses)}
            ORDER BY id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            context = json.loads(row["context"] or "{}")
        except Exception:
            context = {}
        out.append(
            {
                "id": int(row["id"]),
                "ts": float(row["ts"] or 0),
                "level": str(row["level"] or ""),
                "source": str(row["source"] or ""),
                "message": str(row["message"] or ""),
                "context": context if isinstance(context, dict) else {},
                "trace_id": str(row["trace_id"] or ""),
            }
        )
    return out


def prune_old_entries(*, retention_days: int = _DEFAULT_RETENTION_DAYS, max_entries: int = _DEFAULT_MAX_ENTRIES) -> int:
    cutoff = time.time() - max(1, int(retention_days or _DEFAULT_RETENTION_DAYS)) * 86400
    max_keep = max(100, int(max_entries or _DEFAULT_MAX_ENTRIES))
    deleted = 0
    with connect_sync() as conn:
        cursor = conn.execute("DELETE FROM plugin_runtime_logs WHERE ts < ?", (cutoff,))
        deleted += int(cursor.rowcount or 0)
        cursor = conn.execute(
            """
            DELETE FROM plugin_runtime_logs
            WHERE id NOT IN (
                SELECT id FROM plugin_runtime_logs ORDER BY id DESC LIMIT ?
            )
            """,
            (max_keep,),
        )
        deleted += int(cursor.rowcount or 0)
        conn.commit()
    return deleted


def maybe_prune(*, config: Any = None, force: bool = False) -> int:
    global _LAST_PRUNE_AT
    now = time.time()
    if not force and _LAST_PRUNE_AT and now - _LAST_PRUNE_AT < 86400:
        return 0
    _LAST_PRUNE_AT = now
    try:
        return prune_old_entries(
            retention_days=retention_days_from_config(config),
            max_entries=max_entries_from_config(config),
        )
    except Exception:
        return 0


def clear_all() -> int:
    with connect_sync() as conn:
        cursor = conn.execute("DELETE FROM plugin_runtime_logs")
        conn.commit()
    return int(cursor.rowcount or 0)


def _format_message(message: Any, args: tuple[Any, ...]) -> str:
    text = str(message or "")
    if not args:
        return text
    try:
        return text % args
    except Exception:
        return " ".join([text, *(str(arg) for arg in args)])


class PluginRuntimeLogger:
    """Logger proxy that persists personification-only logs and delegates to NoneBot's logger."""

    def __init__(self, wrapped: Any, *, config: Any = None, source: str = "personification") -> None:
        self._wrapped = wrapped
        self._config = config
        self._source = source

    def bind(self, **kwargs: Any) -> "PluginRuntimeLogger":
        wrapped = self._wrapped
        if hasattr(wrapped, "bind"):
            try:
                wrapped = wrapped.bind(**kwargs)
            except Exception:
                wrapped = self._wrapped
        source = str(kwargs.get("source") or kwargs.get("name") or self._source)
        return PluginRuntimeLogger(wrapped, config=self._config, source=source)

    def _persist(self, level: str, message: Any, args: tuple[Any, ...], context: dict[str, Any] | None = None) -> None:
        trace_id = ""
        try:
            from .reply_turn_trace import current_trace_id

            trace_id = current_trace_id()
        except Exception:
            trace_id = ""
        record(
            level=level,
            message=_format_message(message, args),
            source=self._source,
            context=context or {},
            trace_id=trace_id,
            min_level=capture_level_from_config(self._config),
        )

    def _delegate(self, name: str, message: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        target = getattr(self._wrapped, name, None)
        if callable(target):
            return target(message, *args, **kwargs)
        return None

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("DEBUG", message, args)
        return self._delegate("debug", message, args, kwargs)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("INFO", message, args)
        return self._delegate("info", message, args, kwargs)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("WARNING", message, args)
        return self._delegate("warning", message, args, kwargs)

    warn = warning

    def error(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("ERROR", message, args)
        return self._delegate("error", message, args, kwargs)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("ERROR", message, args, context={"exception": True})
        return self._delegate("exception", message, args, kwargs)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        self._persist("CRITICAL", message, args)
        return self._delegate("critical", message, args, kwargs)

    def log(self, level: Any, message: Any, *args: Any, **kwargs: Any) -> Any:
        normalized = _normalize_level(level)
        self._persist(normalized, message, args)
        target = getattr(self._wrapped, "log", None)
        if callable(target):
            return target(level, message, *args, **kwargs)
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def wrap_logger(logger: Any, *, config: Any = None, source: str = "personification") -> PluginRuntimeLogger:
    if isinstance(logger, PluginRuntimeLogger):
        return logger
    return PluginRuntimeLogger(logger, config=config, source=source)


__all__ = [
    "PluginRuntimeLogger",
    "clear_all",
    "max_entries_from_config",
    "maybe_prune",
    "prune_old_entries",
    "query_recent",
    "record",
    "retention_days_from_config",
    "sanitize_text",
    "wrap_logger",
]
