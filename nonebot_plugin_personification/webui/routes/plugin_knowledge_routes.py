from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps import AdminIdentity, require_admin


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
            return {"plugins": [], "total": 0, "available": False}
        try:
            index = store.load_index_sync()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"加载索引失败：{exc}")
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
        return {"plugins": items, "total": len(items), "available": True}

    @router.get("/detail/{plugin_name}")
    async def detail(plugin_name: str, _: AdminIdentity = Depends(require_admin)) -> dict:
        store = _knowledge_store(runtime)
        if store is None:
            raise HTTPException(status_code=503, detail="knowledge_store 未就绪")
        try:
            entry = store.load_plugin_entry_sync(plugin_name)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"读取详情失败：{exc}")
        if entry is None:
            raise HTTPException(status_code=404, detail=f"找不到插件 {plugin_name}")
        runtime_snapshot = None
        source_snapshot = None
        try:
            runtime_snapshot = store.load_runtime_snapshot_sync(plugin_name)
        except Exception:
            runtime_snapshot = None
        try:
            source_snapshot = store.load_source_snapshot_sync(plugin_name)
        except Exception:
            source_snapshot = None
        coverage = _source_coverage_payload(entry=entry, source_snapshot=source_snapshot)
        return {
            "plugin_name": plugin_name,
            "entry": entry,
            "runtime_snapshot": runtime_snapshot,
            "source_snapshot": source_snapshot,
            "source_coverage": coverage,
        }

    @router.get("/search")
    async def search(
        q: str = Query("", min_length=0),
        top_k: int = Query(10, ge=1, le=50),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        store = _knowledge_store(runtime)
        if store is None:
            return {"results": [], "query": q, "available": False}
        query = q.strip()
        if not query:
            return {"results": [], "query": q, "available": True}
        try:
            names = store.search_plugins(query, top_k=top_k)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"搜索失败：{exc}")
        return {"results": list(names), "query": q, "available": True}

    return router
