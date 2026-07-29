from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.operation_diagnostics import detail as operation_detail
from ...core.operation_diagnostics import diagnostic, exception_diagnostic, step
from ..deps import AdminIdentity, require_admin


_DIAGNOSTIC_FIELDS = (
    "ok",
    "code",
    "phase",
    "title",
    "message",
    "details",
    "steps",
    "warnings",
    "suggestion",
    "retryable",
    "partial",
    "outcome_unknown",
    "operation_id",
    "trace_id",
)


def _attach_diagnostic(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["diagnostic"] = report
    for field in _DIAGNOSTIC_FIELDS:
        result.setdefault(field, report[field])
    return result


def _log_read_exception(runtime: Any, exc: BaseException, report: dict[str, Any]) -> None:
    logger = getattr(runtime, "logger", None)
    if logger is None:
        return
    try:
        logger.warning(
            f"[webui] plugin knowledge read failed: code={report.get('code', '')} "
            f"exception={type(exc).__name__} trace={report.get('trace_id', '')}"
        )
    except Exception:
        pass


def _read_failure(
    runtime: Any,
    exc: BaseException,
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    step_key: str,
    step_label: str,
) -> HTTPException:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion="请根据 Trace ID 查看脱敏日志；确认知识库文件状态后重试。",
        retryable=True,
    )
    report["code"] = code
    report["steps"] = [
        step(step_key, step_label, "error", "读取异常中断，未向客户端返回 exception text。").to_dict()
    ]
    _log_read_exception(runtime, exc, report)
    return HTTPException(status_code=500, detail=report)


def _store_unavailable_report(*, operation: str) -> dict[str, Any]:
    return diagnostic(
        ok=False,
        code="plugin_knowledge_store_unavailable",
        phase="store_lookup",
        title="插件知识库未就绪",
        message="当前 runtime 尚未提供 knowledge_store。",
        steps=(step("resolve_store", f"准备{operation}", "error", "knowledge_store 不可用。"),),
        suggestion="等待插件启动完成，或检查 runtime 初始化状态后重试。",
        retryable=True,
    )


def _knowledge_store(runtime) -> Any | None:
    """从 runtime_bundle 嵌套路径取 PluginKnowledgeStore 实例。"""
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    deps = getattr(bundle, "reply_processor_deps", None)
    if deps is None:
        return None
    inner = getattr(deps, "runtime", None)
    if inner is None:
        return None
    return getattr(inner, "knowledge_store", None)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value if value not in (None, "") else default)
    except Exception:
        return float(default)


def _source_coverage_payload(
    *,
    meta: dict[str, Any] | None = None,
    entry: dict[str, Any] | None = None,
    source_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for candidate in (source_snapshot, entry, meta):
        if not isinstance(candidate, dict):
            continue
        coverage = candidate.get("source_coverage", {})
        if isinstance(coverage, dict):
            merged.update(coverage)
        for key in (
            "analysis_scope",
            "analysis_strategy",
            "analysis_mode",
            "source_file_count",
            "source_chunk_count",
            "source_chars",
            "module_bundle_count",
            "source_complete",
            "source_truncated",
        ):
            if key in candidate and candidate.get(key) not in (None, ""):
                merged[key] = candidate.get(key)

    source_file_count = _as_int(merged.get("source_file_count"))
    source_chunk_count = _as_int(merged.get("source_chunk_count"))
    analyzed_chunk_count = _as_int(merged.get("unique_analyzed_chunk_count", merged.get("analyzed_chunk_count", source_chunk_count)))
    full_input = bool(
        merged.get("full_input", False)
        or (
            source_chunk_count > 0
            and analyzed_chunk_count >= source_chunk_count
            and not bool(merged.get("source_truncated", False))
        )
    )
    return {
        "analysis_scope": str(merged.get("analysis_scope", "") or "full_readable_python_source"),
        "analysis_strategy": str(merged.get("analysis_strategy", "") or ""),
        "analysis_mode": str(merged.get("analysis_mode", "") or ""),
        "full_input": full_input,
        "source_complete": bool(merged.get("source_complete", True)),
        "source_truncated": bool(merged.get("source_truncated", False)),
        "source_file_count": source_file_count,
        "source_chunk_count": source_chunk_count,
        "source_chars": _as_int(merged.get("source_chars")),
        "analysis_unit_count": _as_int(merged.get("analysis_unit_count")),
        "analyzed_chunk_count": _as_int(merged.get("analyzed_chunk_count", analyzed_chunk_count)),
        "unique_analyzed_chunk_count": analyzed_chunk_count,
        "duplicate_analyzed_chunk_count": _as_int(merged.get("duplicate_analyzed_chunk_count")),
        "module_bundle_count": _as_int(merged.get("module_bundle_count")),
        "coverage_percent": _as_float(merged.get("coverage_percent"), 100.0 if full_input and source_chunk_count else 0.0),
        "note": str(
            merged.get("note", "")
            or "插件知识库读取完整可读 Python 源码；大型插件按模块或 chunk 分批分析，不做抽样。"
        ),
    }


def build_plugin_knowledge_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/plugin-knowledge", tags=["plugin_knowledge"])

    @router.get("/list")
    async def list_all(_: AdminIdentity = Depends(require_admin)) -> dict:
        store = _knowledge_store(runtime)
        if store is None:
            return _attach_diagnostic(
                {"plugins": [], "total": 0, "available": False},
                _store_unavailable_report(operation="插件知识索引"),
            )
        try:
            index = store.load_index_sync()
            plugins_map = index.get("plugins", {}) if isinstance(index, dict) else {}
            items: list[dict[str, Any]] = []
            for name, meta in plugins_map.items():
                if not isinstance(meta, dict):
                    continue
                coverage = _source_coverage_payload(meta=meta)
                items.append(
                    {
                        "plugin_name": str(name),
                        "display_name": str(meta.get("display_name", "") or ""),
                        "summary": str(meta.get("summary", "") or "")[:200],
                        "keywords": list(meta.get("keywords", []) or [])[:8],
                        "category": str(meta.get("category", "") or ""),
                        "has_runtime_data": bool(meta.get("has_runtime_data", False)),
                        "has_source_data": bool(meta.get("has_source_data", False)),
                        "source_file_count": _as_int(meta.get("source_file_count")),
                        "source_chunk_count": _as_int(meta.get("source_chunk_count")),
                        "source_chars": _as_int(meta.get("source_chars")),
                        "analysis_strategy": str(meta.get("analysis_strategy", "") or ""),
                        "analysis_mode": str(meta.get("analysis_mode", "") or ""),
                        "analysis_scope": str(meta.get("analysis_scope", "") or coverage.get("analysis_scope", "")),
                        "source_complete": bool(meta.get("source_complete", coverage.get("source_complete", True))),
                        "source_truncated": bool(meta.get("source_truncated", coverage.get("source_truncated", False))),
                        "source_coverage": coverage,
                        "updated_at": str(meta.get("updated_at", "") or ""),
                    }
                )
            items.sort(key=lambda x: (x["category"] or "~", x["plugin_name"]))
        except Exception as exc:
            raise _read_failure(
                runtime,
                exc,
                code="plugin_knowledge_index_read_failed",
                phase="index_read",
                title="插件知识索引读取失败",
                message="服务器无法安全读取插件知识索引。",
                step_key="read_index",
                step_label="读取插件知识索引",
            ) from exc
        report = diagnostic(
            ok=True,
            code="plugin_knowledge_list_loaded",
            phase="read_complete",
            title="插件知识索引已读取",
            message=f"已读取 {len(items)} 条插件知识索引。",
            details=(operation_detail("插件数量", len(items), "ok"),),
            steps=(
                step("resolve_store", "准备插件知识索引", "ok", "knowledge_store 已就绪。"),
                step("read_index", "读取插件知识索引", "ok", "索引已读取并规范化。"),
            ),
        )
        return _attach_diagnostic({"plugins": items, "total": len(items), "available": True}, report)

    @router.get("/detail/{plugin_name}")
    async def detail(plugin_name: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        store = _knowledge_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail=_store_unavailable_report(operation="插件知识详情"))
        try:
            entry = store.load_plugin_entry_sync(plugin_name)
        except Exception as exc:
            raise _read_failure(
                runtime,
                exc,
                code="plugin_knowledge_entry_read_failed",
                phase="entry_read",
                title="插件知识详情读取失败",
                message="服务器无法安全读取插件知识详情。",
                step_key="read_entry",
                step_label="读取插件知识详情",
            ) from exc
        if entry is None:
            raise HTTPException(
                status_code=404,
                detail=diagnostic(
                    ok=False,
                    code="plugin_knowledge_not_found",
                    phase="entry_lookup",
                    title="找不到插件知识",
                    message="插件知识索引中没有对应详情。",
                    details=(operation_detail("Plugin", plugin_name),),
                    steps=(step("read_entry", "读取插件知识详情", "error", "没有匹配的插件知识条目。"),),
                    suggestion="刷新插件知识列表后，从当前条目重新打开。",
                    retryable=False,
                ),
            )
        runtime_snapshot = None
        source_snapshot = None
        snapshot_failures: list[tuple[str, BaseException]] = []
        try:
            runtime_snapshot = store.load_runtime_snapshot_sync(plugin_name)
        except Exception as exc:
            snapshot_failures.append(("runtime", exc))
        try:
            source_snapshot = store.load_source_snapshot_sync(plugin_name)
        except Exception as exc:
            snapshot_failures.append(("source", exc))
        coverage = _source_coverage_payload(entry=entry, source_snapshot=source_snapshot)
        operation_steps = [step("read_entry", "读取插件知识详情", "ok", "主知识条目已读取。")]
        failed_kinds = {kind for kind, _exc in snapshot_failures}
        operation_steps.extend(
            (
                step(
                    "read_runtime_snapshot",
                    "读取 runtime snapshot",
                    "warn" if "runtime" in failed_kinds else "ok",
                    "runtime snapshot 读取失败，详情以其余数据返回。" if "runtime" in failed_kinds else "runtime snapshot 已读取。",
                ),
                step(
                    "read_source_snapshot",
                    "读取 source snapshot",
                    "warn" if "source" in failed_kinds else "ok",
                    "source snapshot 读取失败，覆盖统计回退到主条目。" if "source" in failed_kinds else "source snapshot 已读取。",
                ),
            )
        )
        trace_id = ""
        if snapshot_failures:
            first_failure = exception_diagnostic(
                snapshot_failures[0][1],
                phase="snapshot_read",
                message="插件知识快照读取发生内部异常。",
            )
            trace_id = str(first_failure.get("trace_id") or "")
            first_failure["code"] = "plugin_knowledge_snapshot_read_failed"
            _log_read_exception(runtime, snapshot_failures[0][1], first_failure)
        report = diagnostic(
            ok=True,
            code="plugin_knowledge_detail_partial" if snapshot_failures else "plugin_knowledge_detail_loaded",
            phase="snapshot_read" if snapshot_failures else "read_complete",
            title="插件知识详情已部分读取" if snapshot_failures else "插件知识详情已读取",
            message=(
                "主知识条目可用，但部分 snapshot 无法读取。"
                if snapshot_failures
                else "主知识条目与可用 snapshot 已读取。"
            ),
            details=(
                operation_detail("Plugin", plugin_name),
                *(
                    operation_detail(f"{kind} snapshot 异常类型", type(exc).__name__, "warn")
                    for kind, exc in snapshot_failures
                ),
            ),
            steps=tuple(operation_steps),
            warnings=("部分 snapshot 不可用；页面展示的数据可能不完整。",) if snapshot_failures else (),
            suggestion="根据 Trace ID 检查 snapshot 文件后刷新详情。" if snapshot_failures else "",
            partial=bool(snapshot_failures),
            trace_id=trace_id,
        )
        return _attach_diagnostic(
            {
                "plugin_name": plugin_name,
                "entry": entry,
                "runtime_snapshot": runtime_snapshot,
                "source_snapshot": source_snapshot,
                "source_coverage": coverage,
            },
            report,
        )

    @router.get("/search")
    async def search(
        q: str = Query("", min_length=0),
        top_k: int = Query(10, ge=1, le=50),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _knowledge_store(runtime)
        if store is None:
            return _attach_diagnostic(
                {"results": [], "query": q, "available": False},
                _store_unavailable_report(operation="插件知识搜索"),
            )
        query = q.strip()
        if not query:
            report = diagnostic(
                ok=True,
                code="plugin_knowledge_search_skipped",
                phase="query_validation",
                title="插件知识搜索未执行",
                message="搜索词为空，已返回空结果。",
                steps=(step("validate_query", "校验搜索词", "skipped", "没有可执行的搜索词。"),),
            )
            return _attach_diagnostic({"results": [], "query": q, "available": True}, report)
        try:
            names = store.search_plugins(query, top_k=top_k)
            results = list(names)
        except Exception as exc:
            raise _read_failure(
                runtime,
                exc,
                code="plugin_knowledge_search_failed",
                phase="index_search",
                title="插件知识搜索失败",
                message="服务器无法安全搜索插件知识索引。",
                step_key="search_index",
                step_label="搜索插件知识索引",
            ) from exc
        report = diagnostic(
            ok=True,
            code="plugin_knowledge_search_complete",
            phase="read_complete",
            title="插件知识搜索完成",
            message=f"已找到 {len(results)} 条匹配结果。",
            details=(operation_detail("结果数量", len(results), "ok"),),
            steps=(
                step("validate_query", "校验搜索词", "ok", "搜索词有效。"),
                step("search_index", "搜索插件知识索引", "ok", "索引搜索已完成。"),
            ),
        )
        return _attach_diagnostic({"results": results, "query": q, "available": True}, report)

    return router
