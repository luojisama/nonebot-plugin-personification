from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core import (
    config_registry,
    env_writer,
    remote_skill_review,
    skill_overrides,
    webui_audit_log,
)
from ...skill_runtime.source_resolver import parse_skill_sources
from ..deps import AdminIdentity, require_admin


class _NullLogger:
    def info(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _tool_registry(runtime) -> Any | None:
    bundle = getattr(runtime, "runtime_bundle", None)
    if bundle is None:
        return None
    return getattr(bundle, "tool_registry", None)


def _logger(runtime: Any) -> Any:
    return getattr(runtime, "logger", None) or _NullLogger()


def _plugin_config(runtime: Any) -> Any:
    return getattr(runtime, "plugin_config", None)


def _source_dedupe_key(source: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(source.get("source") or "").strip(),
        str(source.get("ref") or "").strip(),
        str(source.get("subdir") or "").strip(),
        str(source.get("kind") or "auto").strip().lower() or "auto",
    )


def _status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"approve", "approved", "同意", "批准", "通过"}:
        return "approved"
    if normalized in {"reject", "rejected", "拒绝", "驳回"}:
        return "rejected"
    if normalized in {"pending", "reset", "待审批", "重置"}:
        return "pending"
    raise HTTPException(status_code=400, detail="status 仅支持 approved / rejected / pending")


def _runtime_reload_available(runtime: Any) -> bool:
    bundle = getattr(runtime, "runtime_bundle", None)
    return callable(getattr(bundle, "reload_runtime_services", None))


def _mcp_tools_payload() -> list[dict[str, Any]]:
    try:
        from ...skill_runtime.mcp_compat import _REGISTERED_MCP_TOOLS
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for name, config in sorted(_REGISTERED_MCP_TOOLS.items(), key=lambda pair: pair[0]):
        if not isinstance(config, dict):
            continue
        items.append(
            {
                "name": str(name),
                "remote_name": str(config.get("remote_name") or ""),
                "command": str(config.get("command") or ""),
                "args_count": len(config.get("args") or []),
                "cwd": str(config.get("cwd") or ""),
                "timeout": int(config.get("timeout") or 0),
                "env_count": len(config.get("env") or {}),
            }
        )
    return items


def _remote_sources_payload(runtime: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cfg = _plugin_config(runtime)
    raw_sources = getattr(cfg, "personification_skill_sources", None)
    logger = _logger(runtime)
    try:
        sources = parse_skill_sources(raw_sources, logger)
    except Exception:
        sources = []
    try:
        reviews = remote_skill_review.list_remote_skill_reviews(raw_sources, logger)
        stats = remote_skill_review.get_remote_skill_review_stats(raw_sources, logger)
    except Exception:
        reviews = []
        stats = {"total": len(sources), "pending": 0, "approved": 0, "rejected": 0}

    review_by_key = {
        _source_dedupe_key(item): item
        for item in reviews
        if isinstance(item, dict)
    }
    items: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        review = review_by_key.get(_source_dedupe_key(source), {})
        enabled = bool(source.get("enabled", True))
        status = str(review.get("status") or ("pending" if enabled else "disabled"))
        items.append(
            {
                "index": index,
                "key": str(review.get("key") or ""),
                "name": str(source.get("name") or ""),
                "source": str(source.get("source") or ""),
                "ref": str(source.get("ref") or ""),
                "subdir": str(source.get("subdir") or ""),
                "kind": str(source.get("kind") or "auto"),
                "enabled": enabled,
                "status": status,
                "last_seen_at": float(review.get("last_seen_at") or 0),
                "reviewed_by": str(review.get("reviewed_by") or ""),
            }
        )
    return items, stats


def _persist_config_value(runtime: Any, field_name: str, value: Any) -> dict[str, Any]:
    cfg = _plugin_config(runtime)
    entry = config_registry.resolve_config_entry(field_name)
    normalized = entry.normalize_value(value) if entry is not None else value
    result = env_writer.write_both(field_name, normalized, cfg)
    try:
        setattr(cfg, field_name, normalized)
    except Exception as exc:
        result.setdefault("errors", []).append(f"运行时同步失败：{exc}")
    if result.get("errors"):
        raise HTTPException(status_code=500, detail="；".join(result["errors"]))
    return result


def _normalize_remote_source_body(body: dict[str, Any]) -> dict[str, Any]:
    source = str(body.get("source") or body.get("url") or body.get("path") or "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    if len(source) > 2048:
        raise HTTPException(status_code=400, detail="source 过长")
    entry = {
        "name": str(body.get("name") or "").strip(),
        "source": source,
        "ref": str(body.get("ref") or "").strip(),
        "subdir": str(body.get("subdir") or "").strip(),
        "kind": str(body.get("kind") or "auto").strip().lower() or "auto",
        "enabled": bool(body.get("enabled", True)),
    }
    parsed_url = urlparse(source)
    host = (parsed_url.hostname or "").lower()
    parts = [part for part in parsed_url.path.strip("/").split("/") if part]
    if "github.com" in host and len(parts) >= 4 and parts[2] == "tree":
        if not entry["ref"]:
            entry["ref"] = parts[3]
        if not entry["subdir"] and len(parts) > 4:
            entry["subdir"] = "/".join(parts[4:])
    parsed = parse_skill_sources([entry], _NullLogger())
    if not parsed:
        raise HTTPException(status_code=400, detail="远程 Skill 源格式不合法")
    return parsed[0]


def build_skill_router(*, runtime) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    async def list_skills(_: AdminIdentity = Depends(require_admin)) -> dict:
        registry = _tool_registry(runtime)
        remote_sources, review_stats = _remote_sources_payload(runtime)
        mcp_tools = _mcp_tools_payload()
        cfg = _plugin_config(runtime)
        summary = {
            "total": 0,
            "active": 0,
            "disabled_by_config": 0,
            "user_disabled": 0,
            "mcp_tools": len(mcp_tools),
            "remote_enabled": bool(getattr(cfg, "personification_skill_remote_enabled", False)),
            "remote_sources": len(remote_sources),
            "remote_sources_enabled": len([item for item in remote_sources if item.get("enabled")]),
            "remote_pending": int(review_stats.get("pending", 0)),
            "remote_approved": int(review_stats.get("approved", 0)),
            "remote_rejected": int(review_stats.get("rejected", 0)),
            "require_admin_review": bool(getattr(cfg, "personification_skill_require_admin_review", True)),
            "allow_unsafe_external": bool(getattr(cfg, "personification_skill_allow_unsafe_external", False)),
            "use_skillpacks": bool(getattr(cfg, "personification_use_skillpacks", False)),
            "reload_available": _runtime_reload_available(runtime),
        }
        if registry is None:
            return {
                "skills": [],
                "available": False,
                "summary": summary,
                "remote_sources": remote_sources,
                "mcp_tools": mcp_tools,
            }
        overrides = skill_overrides.list_overrides()
        try:
            from ...core.tool_health import get_tool_health_statuses

            health_by_name = {item.get("name"): item for item in get_tool_health_statuses()}
        except Exception:
            health_by_name = {}
        skills = []
        mcp_names = {item["name"] for item in mcp_tools}
        for tool in registry.all():
            override = overrides.get(tool.name, {})
            try:
                enabled_by_config = bool(tool.enabled())
            except Exception:
                enabled_by_config = False
            user_disabled = bool(override.get("disabled", False))
            metadata = dict(tool.metadata or {})
            is_mcp = tool.name in mcp_names or str(metadata.get("category") or "").lower() == "mcp"
            health = health_by_name.get(tool.name) or {}
            health_disabled = bool(health and not health.get("available", True))
            skills.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "category": (tool.metadata or {}).get("category", ""),
                    "source_kind": metadata.get("source_kind", "mcp" if is_mcp else ""),
                    "local": bool(getattr(tool, "local", True)),
                    "mcp": bool(is_mcp),
                    "enabled_by_config": enabled_by_config,
                    "user_disabled": user_disabled,
                    "health_disabled": health_disabled,
                    "health": health,
                    "reason": override.get("reason", ""),
                }
            )
            summary["total"] += 1
            if not enabled_by_config:
                summary["disabled_by_config"] += 1
            if user_disabled:
                summary["user_disabled"] += 1
            if health_disabled:
                summary["health_disabled"] = int(summary.get("health_disabled", 0) or 0) + 1
            if enabled_by_config and not user_disabled and not health_disabled:
                summary["active"] += 1
        skills.sort(key=lambda x: (x["category"], x["name"]))
        return {
            "skills": skills,
            "available": True,
            "summary": summary,
            "remote_sources": remote_sources,
            "mcp_tools": mcp_tools,
        }

    @router.post("/{name}/toggle")
    async def toggle(
        name: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        registry = _tool_registry(runtime)
        if registry is None:
            raise HTTPException(status_code=503, detail="tool_registry 未就绪")
        tool = registry.get(name)
        if tool is None:
            raise HTTPException(status_code=404, detail=f"无此 skill：{name}")
        disabled = bool(body.get("disabled", False))
        reason = str(body.get("reason", "") or "")
        skill_overrides.set_disabled(name, disabled, reason=reason)
        webui_audit_log.record(
            action="skill_toggle",
            qq=admin.qq,
            device_id=admin.device_id,
            target=name,
            detail={"disabled": disabled, "reason": reason},
        )
        return {"success": True, "name": name, "user_disabled": disabled}

    @router.post("/remote/review")
    async def review_remote(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        selector = str(body.get("selector") or body.get("key") or "pending").strip() or "pending"
        status = _status_label(str(body.get("status") or ""))
        raw_sources = getattr(_plugin_config(runtime), "personification_skill_sources", None)
        matched_count, matched_items = remote_skill_review.review_remote_skill_sources(
            raw_sources,
            _logger(runtime),
            selector=selector,
            status=status,
            operator=admin.qq,
        )
        if matched_count <= 0:
            raise HTTPException(status_code=404, detail="没有匹配的远程 Skill 源")
        webui_audit_log.record(
            action="remote_skill_review",
            qq=admin.qq,
            device_id=admin.device_id,
            target=selector,
            detail={"status": status, "matched_count": matched_count},
        )
        return {"success": True, "matched_count": matched_count, "items": matched_items}

    @router.post("/remote/source")
    async def add_remote_source(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        entry = _normalize_remote_source_body(body)
        cfg = _plugin_config(runtime)
        logger = _logger(runtime)
        current = parse_skill_sources(getattr(cfg, "personification_skill_sources", None), logger)
        prefer_first = bool(body.get("prefer_first", False))
        auto_approve = bool(body.get("auto_approve", False))
        dedupe_key = _source_dedupe_key(entry)
        existing_index = next(
            (idx for idx, item in enumerate(current) if _source_dedupe_key(item) == dedupe_key),
            -1,
        )
        changed = False
        if existing_index >= 0:
            merged = dict(current[existing_index])
            merged.update(entry)
            if merged != current[existing_index]:
                current[existing_index] = merged
                changed = True
            if prefer_first and existing_index != 0:
                item = current.pop(existing_index)
                current.insert(0, item)
                changed = True
        else:
            if prefer_first:
                current.insert(0, entry)
            else:
                current.append(entry)
            changed = True

        config_results = []
        config_results.append(_persist_config_value(runtime, "personification_skill_sources", current))
        if bool(body.get("enable_remote", True)):
            config_results.append(_persist_config_value(runtime, "personification_skill_remote_enabled", True))
        # 远程源默认保留审核门禁；管理员可在配置中心单独关闭。
        if not hasattr(cfg, "personification_skill_require_admin_review"):
            config_results.append(_persist_config_value(runtime, "personification_skill_require_admin_review", True))

        approved_count = 0
        if auto_approve:
            reviews = remote_skill_review.list_remote_skill_reviews(
                getattr(cfg, "personification_skill_sources", None),
                logger,
            )
            review_key = ""
            for item in reviews:
                if _source_dedupe_key(item) == dedupe_key:
                    review_key = str(item.get("key") or "")
                    break
            if review_key:
                approved_count, _items = remote_skill_review.review_remote_skill_sources(
                    getattr(cfg, "personification_skill_sources", None),
                    logger,
                    selector=review_key,
                    status="approved",
                    operator=admin.qq,
                )

        webui_audit_log.record(
            action="remote_skill_source_add",
            qq=admin.qq,
            device_id=admin.device_id,
            target=entry["source"],
            detail={
                "name": entry.get("name", ""),
                "ref": entry.get("ref", ""),
                "subdir": entry.get("subdir", ""),
                "changed": changed,
                "auto_approve": auto_approve,
                "approved_count": approved_count,
            },
        )
        return {
            "success": True,
            "changed": changed,
            "source": entry,
            "auto_approved": approved_count > 0,
            "needs_reload": True,
            "config_results": config_results,
        }

    @router.post("/reload")
    async def reload_skills(admin: AdminIdentity = Depends(require_admin)) -> dict:
        bundle = getattr(runtime, "runtime_bundle", None)
        reload_services = getattr(bundle, "reload_runtime_services", None)
        if not callable(reload_services):
            raise HTTPException(status_code=503, detail="当前运行时未提供重载器")
        try:
            reload_services()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"重载失败：{exc}") from exc
        webui_audit_log.record(
            action="skill_runtime_reload",
            qq=admin.qq,
            device_id=admin.device_id,
        )
        return {"success": True}

    return router
