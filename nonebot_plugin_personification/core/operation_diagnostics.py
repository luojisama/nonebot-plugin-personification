from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from .sensitive_data import sanitize_object, sanitize_text


_DETAIL_STATUSES = {"info", "ok", "warn", "error"}
_STEP_STATUSES = {"pending", "running", "ok", "skipped", "warn", "error", "unknown"}


def _safe_text(value: Any, *, limit: int = 500) -> str:
    return sanitize_text(str(value or ""), limit=max(1, int(limit)))


@dataclass(frozen=True, slots=True)
class OperationDetail:
    label: str
    value: Any
    status: str = "info"

    def to_dict(self) -> dict[str, Any]:
        status = self.status if self.status in _DETAIL_STATUSES else "info"
        return {
            "label": _safe_text(self.label, limit=80),
            "value": sanitize_object(self.value),
            "status": status,
        }


@dataclass(frozen=True, slots=True)
class OperationStep:
    key: str
    label: str
    status: str
    message: str = ""
    details: tuple[OperationDetail, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        status = self.status if self.status in _STEP_STATUSES else "unknown"
        return {
            "key": _safe_text(self.key, limit=64),
            "label": _safe_text(self.label, limit=120),
            "status": status,
            "message": _safe_text(self.message, limit=500),
            "details": [item.to_dict() for item in self.details],
        }


@dataclass(frozen=True, slots=True)
class OperationDiagnostic:
    ok: bool
    code: str
    phase: str
    title: str
    message: str
    details: tuple[OperationDetail, ...] = ()
    steps: tuple[OperationStep, ...] = ()
    warnings: tuple[str, ...] = ()
    suggestion: str = ""
    retryable: bool = False
    partial: bool = False
    outcome_unknown: bool = False
    operation_id: str = ""
    trace_id: str = ""

    def to_dict(self, *, include_error_alias: bool = True) -> dict[str, Any]:
        payload = {
            "ok": bool(self.ok),
            "code": _safe_text(self.code, limit=80) or ("ok" if self.ok else "operation_failed"),
            "phase": _safe_text(self.phase, limit=80),
            "title": _safe_text(self.title, limit=160),
            "message": _safe_text(self.message, limit=800),
            "details": [item.to_dict() for item in self.details],
            "steps": [item.to_dict() for item in self.steps],
            "warnings": [_safe_text(item, limit=500) for item in self.warnings if str(item or "").strip()],
            "suggestion": _safe_text(self.suggestion, limit=800),
            "retryable": bool(self.retryable),
            "partial": bool(self.partial),
            "outcome_unknown": bool(self.outcome_unknown),
            "operation_id": _safe_text(self.operation_id, limit=128),
            "trace_id": _safe_text(self.trace_id, limit=128),
        }
        if include_error_alias and not self.ok:
            payload["error"] = payload["message"] or payload["title"]
        return payload


def detail(label: str, value: Any, status: str = "info") -> OperationDetail:
    return OperationDetail(label=label, value=value, status=status)


def step(
    key: str,
    label: str,
    status: str,
    message: str = "",
    *,
    details: Iterable[OperationDetail] = (),
) -> OperationStep:
    return OperationStep(
        key=key,
        label=label,
        status=status,
        message=message,
        details=tuple(details),
    )


def diagnostic(
    *,
    ok: bool,
    code: str,
    phase: str,
    title: str,
    message: str,
    details: Iterable[OperationDetail] = (),
    steps: Iterable[OperationStep] = (),
    warnings: Iterable[str] = (),
    suggestion: str = "",
    retryable: bool = False,
    partial: bool = False,
    outcome_unknown: bool = False,
    operation_id: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    return OperationDiagnostic(
        ok=ok,
        code=code,
        phase=phase,
        title=title,
        message=message,
        details=tuple(details),
        steps=tuple(steps),
        warnings=tuple(warnings),
        suggestion=suggestion,
        retryable=retryable,
        partial=partial,
        outcome_unknown=outcome_unknown,
        operation_id=operation_id,
        trace_id=trace_id,
    ).to_dict()


def exception_diagnostic(
    exc: BaseException,
    *,
    phase: str,
    title: str = "操作未完成",
    message: str = "操作过程中发生内部异常。",
    suggestion: str = "请根据 Trace ID 查看脱敏日志；确认状态后再决定是否重试。",
    operation_id: str = "",
    trace_id: str = "",
    retryable: bool | None = None,
) -> dict[str, Any]:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        code = "operation_timeout"
        safe_message = "操作超过等待时间，尚未得到明确完成结果。"
        inferred_retryable = True
    elif isinstance(exc, PermissionError):
        code = "permission_denied"
        safe_message = "服务器没有完成该操作所需的权限。"
        inferred_retryable = False
    elif isinstance(exc, FileNotFoundError):
        code = "resource_not_found"
        safe_message = "操作依赖的文件或资源不存在。"
        inferred_retryable = False
    elif isinstance(exc, ValueError):
        code = "invalid_input"
        safe_message = message or "输入或返回数据不符合要求。"
        inferred_retryable = False
    elif isinstance(exc, OSError):
        code = "io_failed"
        safe_message = "服务器读写资源时失败。"
        inferred_retryable = True
    else:
        code = "internal_error"
        safe_message = message
        inferred_retryable = True
    return diagnostic(
        ok=False,
        code=code,
        phase=phase,
        title=title,
        message=safe_message,
        details=(detail("异常类型", type(exc).__name__, "error"),),
        suggestion=suggestion,
        retryable=inferred_retryable if retryable is None else retryable,
        operation_id=operation_id,
        trace_id=trace_id or uuid.uuid4().hex,
    )


def normalize_diagnostic(value: Mapping[str, Any] | None, *, ok: bool | None = None) -> dict[str, Any]:
    source = dict(value or {})
    resolved_ok = bool(source.get("ok")) if ok is None else bool(ok)
    details: list[OperationDetail] = []
    for item in source.get("details") or []:
        if isinstance(item, Mapping):
            details.append(detail(str(item.get("label") or "详情"), item.get("value"), str(item.get("status") or "info")))
    steps: list[OperationStep] = []
    for item in source.get("steps") or []:
        if not isinstance(item, Mapping):
            continue
        nested = []
        for child in item.get("details") or []:
            if isinstance(child, Mapping):
                nested.append(detail(str(child.get("label") or "详情"), child.get("value"), str(child.get("status") or "info")))
        steps.append(
            step(
                str(item.get("key") or "step"),
                str(item.get("label") or "步骤"),
                str(item.get("status") or "unknown"),
                str(item.get("message") or ""),
                details=nested,
            )
        )
    return diagnostic(
        ok=resolved_ok,
        code=str(source.get("code") or ("ok" if resolved_ok else "operation_failed")),
        phase=str(source.get("phase") or ""),
        title=str(source.get("title") or ("操作完成" if resolved_ok else "操作未完成")),
        message=str(source.get("message") or source.get("error") or ""),
        details=details,
        steps=steps,
        warnings=source.get("warnings") or (),
        suggestion=str(source.get("suggestion") or ""),
        retryable=bool(source.get("retryable")),
        partial=bool(source.get("partial")),
        outcome_unknown=bool(source.get("outcome_unknown")),
        operation_id=str(source.get("operation_id") or ""),
        trace_id=str(source.get("trace_id") or ""),
    )


__all__ = [
    "OperationDetail",
    "OperationDiagnostic",
    "OperationStep",
    "detail",
    "diagnostic",
    "exception_diagnostic",
    "normalize_diagnostic",
    "step",
]
