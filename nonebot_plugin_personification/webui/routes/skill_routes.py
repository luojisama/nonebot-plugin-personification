from __future__ import annotations

import inspect
from typing import Any
from urllib.parse import parse_qsl, urlparse

from fastapi import APIRouter, Body, Depends, HTTPException

from ...core import (
    config_registry,
    env_writer,
    remote_skill_review,
    skill_overrides,
    webui_audit_log,
)
from ...core.operation_diagnostics import detail, diagnostic, exception_diagnostic, step
from ...core.paths import get_data_dir
from ...skill_runtime.source_resolver import parse_skill_sources
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


class _ConfigPersistenceError(RuntimeError):
    def __init__(self, field_name: str, *, persisted: bool, runtime_synced: bool) -> None:
        super().__init__(field_name)
        self.field_name = field_name
        self.persisted = persisted
        self.runtime_synced = runtime_synced


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


def _attach_diagnostic(payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["diagnostic"] = report
    for field in _DIAGNOSTIC_FIELDS:
        result.setdefault(field, report[field])
    return result


def _source_descriptor(source: dict[str, Any]) -> str:
    raw = str(source.get("source") or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return "configured source"
    if parsed.scheme.lower() in {"http", "https"}:
        host = (parsed.hostname or "unknown-host").lower()
        return f"{parsed.scheme.lower()} remote ({host})"
    if raw.lower().endswith(".zip"):
        return "local zip"
    return "local path"


def _exception_report(
    exc: BaseException,
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    steps: tuple,
    retryable: bool,
    partial: bool = False,
    outcome_unknown: bool = False,
) -> dict[str, Any]:
    report = exception_diagnostic(
        exc,
        phase=phase,
        title=title,
        message=message,
        suggestion=suggestion,
        retryable=retryable,
    )
    report["code"] = code
    report["steps"] = [item.to_dict() for item in steps]
    report["partial"] = partial
    report["outcome_unknown"] = outcome_unknown
    return report


def _validation_error(
    *,
    code: str,
    phase: str,
    title: str,
    message: str,
    suggestion: str,
    step_key: str,
    step_label: str,
) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=diagnostic(
            ok=False,
            code=code,
            phase=phase,
            title=title,
            message=message,
            steps=(step(step_key, step_label, "error", message),),
            suggestion=suggestion,
            retryable=True,
        ),
    )


def _status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"approve", "approved", "同意", "批准", "通过"}:
        return "approved"
    if normalized in {"reject", "rejected", "拒绝", "驳回"}:
        return "rejected"
    if normalized in {"pending", "reset", "待审批", "重置"}:
        return "pending"
    raise _validation_error(
        code="remote_skill_review_status_invalid",
        phase="request_validation",
        title="远程 Skill 审核状态无效",
        message="status 仅支持 approved、rejected 或 pending。",
        suggestion="选择页面提供的审核状态后重新提交。",
        step_key="validate_review",
        step_label="校验审核请求",
    )


def _runtime_reload_available(runtime: Any) -> bool:
    bundle = getattr(runtime, "runtime_bundle", None)
    return callable(getattr(bundle, "reload_all_runtime_services", None)) or callable(
        getattr(bundle, "reload_runtime_services", None)
    )


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
                "content_digest": str(review.get("content_digest") or ""),
                "approved_digest": str(review.get("approved_digest") or ""),
            }
        )
    return items, stats


def _persist_config_value(runtime: Any, field_name: str, value: Any) -> dict[str, Any]:
    cfg = _plugin_config(runtime)
    try:
        entry = config_registry.resolve_config_entry(field_name)
        normalized = entry.normalize_value(value) if entry is not None else value
    except Exception as exc:
        raise _ConfigPersistenceError(field_name, persisted=False, runtime_synced=False) from exc
    try:
        result = env_writer.write_both(field_name, normalized, cfg)
    except Exception as exc:
        raise _ConfigPersistenceError(field_name, persisted=False, runtime_synced=False) from exc
    persisted = bool(result.get("env_json_path")) and not bool(result.get("errors"))
    if not persisted:
        raise _ConfigPersistenceError(field_name, persisted=False, runtime_synced=False)
    try:
        setattr(cfg, field_name, normalized)
    except Exception as exc:
        raise _ConfigPersistenceError(field_name, persisted=True, runtime_synced=False) from exc
    return result


def _normalize_remote_source_body(body: dict[str, Any]) -> dict[str, Any]:
    source = str(body.get("source") or body.get("url") or body.get("path") or "").strip()
    if not source:
        raise _validation_error(
            code="remote_skill_source_required",
            phase="source_parse",
            title="缺少远程 Skill 来源",
            message="source 不能为空。",
            suggestion="填写 GitHub、zip 或服务器本地目录来源。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        )
    if len(source) > 2048:
        raise _validation_error(
            code="remote_skill_source_too_long",
            phase="source_parse",
            title="远程 Skill 来源过长",
            message="source 超过允许长度。",
            suggestion="使用不含临时参数的仓库、zip 或本地目录地址。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        )
    entry = {
        "name": str(body.get("name") or "").strip(),
        "source": source,
        "ref": str(body.get("ref") or "").strip(),
        "subdir": str(body.get("subdir") or "").strip(),
        "kind": str(body.get("kind") or "auto").strip().lower() or "auto",
        "enabled": bool(body.get("enabled", True)),
    }
    try:
        parsed_url = urlparse(source)
    except ValueError as exc:
        raise _validation_error(
            code="remote_skill_source_invalid",
            phase="source_parse",
            title="远程 Skill 来源格式无效",
            message="source 不是可解析的 URL 或本地路径。",
            suggestion="检查 URL 括号、scheme 与路径格式后重试。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        ) from exc
    if parsed_url.username is not None or parsed_url.password is not None:
        raise _validation_error(
            code="remote_skill_source_credentials_forbidden",
            phase="source_parse",
            title="远程 Skill 来源包含凭据",
            message="source 不允许在 URL 中携带用户名、密码或其它凭据。",
            suggestion="改用不含凭据的公开地址，并通过服务器侧安全配置提供认证。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        )
    credential_query_keys = {
        "access_token",
        "api_key",
        "apikey",
        "key",
        "password",
        "passwd",
        "secret",
        "signature",
        "token",
    }
    if any(
        key.strip().lower().replace("-", "_") in credential_query_keys
        for key, _value in parse_qsl(parsed_url.query, keep_blank_values=True)
    ):
        raise _validation_error(
            code="remote_skill_source_credentials_forbidden",
            phase="source_parse",
            title="远程 Skill 来源包含凭据",
            message="source 不允许在 URL query 中携带 token、password、secret 或签名参数。",
            suggestion="改用不含凭据的公开地址，并通过服务器侧安全配置提供认证。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        )
    host = (parsed_url.hostname or "").lower()
    parts = [part for part in parsed_url.path.strip("/").split("/") if part]
    if "github.com" in host and len(parts) >= 4 and parts[2] == "tree":
        if not entry["ref"]:
            entry["ref"] = parts[3]
        if not entry["subdir"] and len(parts) > 4:
            entry["subdir"] = "/".join(parts[4:])
    try:
        parsed = parse_skill_sources([entry], _NullLogger())
    except Exception as exc:
        report = _exception_report(
            exc,
            code="remote_skill_source_parse_failed",
            phase="source_parse",
            title="远程 Skill 来源解析失败",
            message="服务器无法解析该来源配置。",
            suggestion="检查来源类型、ref 与 subdir 后重试。",
            steps=(step("parse_source", "解析远程 Skill 来源", "error", "来源配置未能规范化。"),),
            retryable=True,
        )
        raise HTTPException(status_code=400, detail=report) from exc
    if not parsed:
        raise _validation_error(
            code="remote_skill_source_invalid",
            phase="source_parse",
            title="远程 Skill 来源格式无效",
            message="来源配置未能解析为有效的 Skill source。",
            suggestion="检查来源类型、ref 与 subdir 后重试。",
            step_key="parse_source",
            step_label="解析远程 Skill 来源",
        )
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
        summary["mcp_tools"] = len(mcp_names | {str(item["name"]) for item in skills if item.get("mcp")})
        return {
            "skills": skills,
            "available": True,
            "summary": summary,
            "remote_sources": remote_sources,
            "mcp_tools": mcp_tools,
        }

    @router.post("/remote/toggle")
    async def toggle_remote_loading(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        enabled = bool(body.get("enabled", False))
        validation_step = step(
            "validate_remote_toggle",
            "校验远程 Skill 开关",
            "ok",
            "已确认目标配置字段与请求状态。",
        )
        try:
            config_result = _persist_config_value(
                runtime,
                "personification_skill_remote_enabled",
                enabled,
            )
        except _ConfigPersistenceError as exc:
            partial = bool(exc.persisted)
            report = _exception_report(
                exc,
                code="remote_skill_toggle_partial" if partial else "remote_skill_toggle_persist_failed",
                phase="config_persistence",
                title="远程 Skill 开关仅部分保存" if partial else "远程 Skill 开关保存失败",
                message=(
                    "配置已写入持久层，但当前运行时对象未同步。"
                    if partial
                    else "远程 Skill 开关未能写入持久配置。"
                ),
                suggestion=(
                    "先刷新配置状态并重启或重载 runtime；确认状态前不要重复切换。"
                    if partial
                    else "检查 env.json 的写入权限后重试。"
                ),
                steps=(
                    validation_step,
                    step(
                        "persist_remote_toggle",
                        "持久化远程 Skill 开关",
                        "warn" if partial else "error",
                        "持久层已写入，但运行时同步失败。" if partial else "持久配置未写入。",
                    ),
                ),
                retryable=not partial,
                partial=partial,
            )
            webui_audit_log.record(
                action="remote_skill_toggle",
                qq=admin.qq,
                device_id=admin.device_id,
                target="remote_loading",
                detail={"enabled": enabled, "code": report["code"], "phase": report["phase"]},
                outcome="error",
            )
            raise HTTPException(status_code=500, detail=report) from exc
        report = diagnostic(
            ok=True,
            code="remote_skill_loading_enabled" if enabled else "remote_skill_loading_disabled",
            phase="config_persisted",
            title="远程 Skill 加载已开启" if enabled else "远程 Skill 加载已关闭",
            message="开关已写入持久配置；重载 Skill runtime 后，已注册工具才会与配置一致。",
            details=(
                detail("目标状态", "enabled" if enabled else "disabled", "ok"),
                detail("持久化", "env.json", "ok"),
            ),
            steps=(
                validation_step,
                step("persist_remote_toggle", "持久化远程 Skill 开关", "ok", "配置与当前 runtime config 已同步。"),
                step("reload_runtime", "重载 Skill runtime", "pending", "尚未执行 runtime reload。"),
            ),
            suggestion="执行一次 Skill runtime reload 使工具注册状态与新开关一致。",
        )
        webui_audit_log.record(
            action="remote_skill_toggle",
            qq=admin.qq,
            device_id=admin.device_id,
            target="remote_loading",
            detail={"enabled": enabled, "code": report["code"]},
        )
        return _attach_diagnostic(
            {
                "success": True,
                "enabled": enabled,
                "needs_reload": True,
                "config_result": config_result,
            },
            report,
        )

    @router.post("/{name}/toggle")
    async def toggle(
        name: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        registry = _tool_registry(runtime)
        if registry is None:
            raise HTTPException(
                status_code=503,
                detail=diagnostic(
                    ok=False,
                    code="skill_registry_unavailable",
                    phase="registry_lookup",
                    title="Skill registry 未就绪",
                    message="当前 runtime 尚未提供可操作的 tool registry。",
                    steps=(step("resolve_skill", "读取 Skill registry", "error", "tool_registry 不可用。"),),
                    suggestion="等待插件启动完成，或检查 runtime 初始化状态后重试。",
                    retryable=True,
                ),
            )
        tool = registry.get(name)
        if tool is None:
            raise HTTPException(
                status_code=404,
                detail=diagnostic(
                    ok=False,
                    code="skill_not_found",
                    phase="registry_lookup",
                    title="找不到指定 Skill",
                    message="当前 tool registry 中没有该 Skill。",
                    details=(detail("Skill", name),),
                    steps=(step("resolve_skill", "定位 Skill", "error", "没有匹配的已注册工具。"),),
                    suggestion="刷新 Skill 页面后从当前列表重新操作。",
                    retryable=False,
                ),
            )
        disabled = bool(body.get("disabled", False))
        reason = str(body.get("reason", "") or "")
        try:
            skill_overrides.set_disabled(name, disabled, reason=reason)
            persisted = bool(skill_overrides.list_overrides().get(name, {}).get("disabled", False))
            if persisted != disabled:
                raise _ConfigPersistenceError("skill_overrides", persisted=False, runtime_synced=False)
        except Exception as exc:
            report = _exception_report(
                exc,
                code="skill_toggle_persist_failed",
                phase="override_persistence",
                title="Skill 开关保存失败",
                message="Skill override 未能确认写入持久存储。",
                suggestion="检查插件数据目录与 DataStore 状态后重试。",
                steps=(
                    step("resolve_skill", "定位 Skill", "ok", "已找到当前注册工具。"),
                    step("persist_override", "持久化 Skill override", "error", "无法确认 override 的最终状态。"),
                ),
                retryable=True,
                outcome_unknown=True,
            )
            webui_audit_log.record(
                action="skill_toggle",
                qq=admin.qq,
                device_id=admin.device_id,
                target=name,
                detail={"disabled": disabled, "code": report["code"], "trace_id": report["trace_id"]},
                outcome="unknown",
            )
            raise HTTPException(status_code=500, detail=report) from exc
        webui_audit_log.record(
            action="skill_toggle",
            qq=admin.qq,
            device_id=admin.device_id,
            target=name,
            detail={"disabled": disabled, "reason_set": bool(reason)},
        )
        report = diagnostic(
            ok=True,
            code="skill_disabled" if disabled else "skill_enabled",
            phase="override_persisted",
            title=f"Skill 已{'禁用' if disabled else '启用'}",
            message="Skill override 已持久化，并已从 DataStore 回读确认。",
            details=(
                detail("Skill", name),
                detail("Override", "disabled" if disabled else "enabled", "ok"),
                detail("持久化校验", "confirmed", "ok"),
            ),
            steps=(
                step("resolve_skill", "定位 Skill", "ok", "已找到当前注册工具。"),
                step("persist_override", "持久化 Skill override", "ok", "DataStore 写入完成。"),
                step("verify_override", "回读 Skill override", "ok", "持久状态与请求一致。"),
            ),
            suggestion="无需 reload；后续 ToolRegistry.active() 会立即应用该 override。",
        )
        return _attach_diagnostic(
            {"success": True, "name": name, "user_disabled": disabled},
            report,
        )

    @router.post("/remote/review")
    async def review_remote(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        selector = str(body.get("selector") or body.get("key") or "pending").strip() or "pending"
        status = _status_label(str(body.get("status") or ""))
        cfg = _plugin_config(runtime)
        raw_sources = getattr(cfg, "personification_skill_sources", None)
        try:
            review_sources = raw_sources
            if status == "approved":
                review_sources = await remote_skill_review.prepare_remote_skill_reviews(
                    raw_sources,
                    cfg,
                    _logger(runtime),
                    data_dir=get_data_dir(cfg),
                )
            matched_count, matched_items = remote_skill_review.review_remote_skill_sources(
                review_sources,
                _logger(runtime),
                selector=selector,
                status=status,
                operator=admin.qq,
            )
        except Exception as exc:
            report = _exception_report(
                exc,
                code="remote_skill_review_outcome_unknown",
                phase="review_persistence",
                title="远程 Skill 审核状态未确认",
                message="审核写入异常中断，当前响应无法确认最终状态。",
                suggestion="先刷新远程源列表核对审核状态；确认前不要重复提交。",
                steps=(
                    step("validate_review", "校验审核请求", "ok", "审核状态与 selector 已通过基础校验。"),
                    step("persist_review", "持久化审核状态", "unknown", "审核存储的最终状态未知。"),
                    step("reload_runtime", "重载 Skill runtime", "skipped", "审核状态未确认，未执行 reload。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
            webui_audit_log.record(
                action="remote_skill_review",
                qq=admin.qq,
                device_id=admin.device_id,
                target="review_selector",
                detail={"status": status, "code": report["code"], "trace_id": report["trace_id"]},
                outcome="unknown",
            )
            raise HTTPException(status_code=500, detail=report) from exc
        if matched_count <= 0:
            raise HTTPException(
                status_code=404,
                detail=diagnostic(
                    ok=False,
                    code="remote_skill_review_target_not_found",
                    phase="review_selection",
                    title="没有匹配的远程 Skill 来源",
                    message="当前远程来源列表中没有符合 selector 的审核项。",
                    steps=(
                        step("validate_review", "校验审核请求", "ok", "审核状态有效。"),
                        step("select_sources", "匹配远程 Skill 来源", "error", "没有找到可更新的审核项。"),
                    ),
                    suggestion="刷新页面后使用来源的审核 key 重试。",
                    retryable=True,
                ),
            )
        matched_keys = [str(item.get("key") or "") for item in matched_items if isinstance(item, dict)]
        webui_audit_log.record(
            action="remote_skill_review",
            qq=admin.qq,
            device_id=admin.device_id,
            target=matched_keys[0] if len(matched_keys) == 1 else "multiple_sources",
            detail={"status": status, "matched_count": matched_count, "keys": matched_keys[:20]},
        )
        report = diagnostic(
            ok=True,
            code=f"remote_skill_review_{status}",
            phase="review_persisted",
            title="远程 Skill 审核状态已更新",
            message=f"已将 {matched_count} 个来源更新为 {status}。",
            details=(
                detail("审核状态", status, "ok"),
                detail("匹配数量", matched_count, "ok"),
            ),
            steps=(
                step("validate_review", "校验审核请求", "ok", "审核状态与 selector 有效。"),
                step("select_sources", "匹配远程 Skill 来源", "ok", f"已匹配 {matched_count} 个来源。"),
                step("persist_review", "持久化审核状态", "ok", "审核状态已写入 DataStore 并回读。"),
                step("reload_runtime", "重载 Skill runtime", "pending", "审核变化尚未应用到 tool registry。"),
            ),
            suggestion="执行一次 Skill runtime reload，使批准或拒绝结果反映到工具注册状态。",
        )
        return _attach_diagnostic(
            {"success": True, "matched_count": matched_count, "items": matched_items},
            report,
        )

    @router.post("/remote/source")
    async def add_remote_source(
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        entry = _normalize_remote_source_body(body)
        cfg = _plugin_config(runtime)
        logger = _logger(runtime)
        parse_step = step(
            "parse_source",
            "解析远程 Skill 来源",
            "ok",
            "来源、ref、subdir 与 kind 已规范化。",
            details=(detail("来源类型", _source_descriptor(entry)), detail("Kind", entry.get("kind") or "auto")),
        )
        try:
            current = parse_skill_sources(getattr(cfg, "personification_skill_sources", None), logger)
        except Exception as exc:
            report = _exception_report(
                exc,
                code="remote_skill_source_config_read_failed",
                phase="source_config_read",
                title="无法读取现有远程 Skill 来源",
                message="新来源已解析，但服务器无法安全读取当前来源配置。",
                suggestion="检查当前 personification_skill_sources 配置后重试。",
                steps=(
                    parse_step,
                    step("read_sources", "读取现有来源配置", "error", "当前来源列表无法解析。"),
                ),
                retryable=True,
            )
            webui_audit_log.record(
                action="remote_skill_source_add",
                qq=admin.qq,
                device_id=admin.device_id,
                target=_source_descriptor(entry),
                detail={"code": report["code"], "trace_id": report["trace_id"]},
                outcome="error",
            )
            raise HTTPException(status_code=500, detail=report) from exc
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

        duplicate = existing_index >= 0
        operation_steps = [
            parse_step,
            step(
                "deduplicate_source",
                "检查重复来源",
                "warn" if duplicate and not changed else "ok",
                (
                    "相同来源已经存在，配置内容与顺序均未变化。"
                    if duplicate and not changed
                    else "已合并现有来源的展示属性或优先级。"
                    if duplicate
                    else "没有发现相同 source/ref/subdir/kind。"
                ),
            ),
        ]
        config_results: list[dict[str, Any]] = []
        persisted_fields: list[str] = []
        config_specs = [
            (
                "personification_skill_sources",
                current,
                "persist_sources",
                "持久化远程来源列表",
            )
        ]
        if bool(body.get("enable_remote", True)):
            config_specs.append(
                (
                    "personification_skill_remote_enabled",
                    True,
                    "enable_remote_loading",
                    "开启远程 Skill 加载",
                )
            )
        else:
            operation_steps.append(
                step("enable_remote_loading", "开启远程 Skill 加载", "skipped", "请求保留当前远程加载开关。")
            )
        # 远程源默认保留审核门禁；管理员可在配置中心单独关闭。
        if not hasattr(cfg, "personification_skill_require_admin_review"):
            config_specs.append(
                (
                    "personification_skill_require_admin_review",
                    True,
                    "persist_review_gate",
                    "持久化管理员审核门禁",
                )
            )
        else:
            operation_steps.append(
                step(
                    "persist_review_gate",
                    "确认管理员审核门禁",
                    "ok",
                    "保留当前审核门禁配置。",
                    details=(
                        detail(
                            "当前状态",
                            "required" if bool(getattr(cfg, "personification_skill_require_admin_review", True)) else "disabled",
                            "ok" if bool(getattr(cfg, "personification_skill_require_admin_review", True)) else "warn",
                        ),
                    ),
                )
            )

        for field_name, value, step_key, step_label in config_specs:
            try:
                config_results.append(_persist_config_value(runtime, field_name, value))
                persisted_fields.append(field_name)
                operation_steps.append(step(step_key, step_label, "ok", "env.json 与当前 runtime config 已同步。"))
            except _ConfigPersistenceError as exc:
                if exc.persisted and field_name not in persisted_fields:
                    persisted_fields.append(field_name)
                partial = bool(persisted_fields)
                operation_steps.append(
                    step(
                        step_key,
                        step_label,
                        "warn" if exc.persisted else "error",
                        (
                            "env.json 已写入，但当前 runtime config 未同步。"
                            if exc.persisted
                            else "该配置字段未能写入 env.json。"
                        ),
                    )
                )
                operation_steps.append(
                    step("auto_approve", "自动批准远程来源", "skipped", "配置持久化未完整完成。")
                )
                operation_steps.append(
                    step("reload_runtime", "重载 Skill runtime", "skipped", "配置状态需要先核对。")
                )
                report = _exception_report(
                    exc,
                    code="remote_skill_source_persistence_partial" if partial else "remote_skill_source_persist_failed",
                    phase="config_persistence",
                    title="远程 Skill 来源仅部分保存" if partial else "远程 Skill 来源保存失败",
                    message=(
                        "部分配置字段已经持久化，其余字段未完成。"
                        if partial
                        else "远程 Skill 来源没有写入持久配置。"
                    ),
                    suggestion=(
                        "先刷新配置并核对已持久化字段，再补齐失败字段；确认前不要 reload。"
                        if partial
                        else "检查 env.json 写入权限后重试。"
                    ),
                    steps=tuple(operation_steps),
                    retryable=not partial,
                    partial=partial,
                )
                report["details"].extend(
                    [
                        detail("来源类型", _source_descriptor(entry)).to_dict(),
                        detail("已持久化字段", persisted_fields or "none", "warn" if partial else "error").to_dict(),
                        detail("失败字段", field_name, "error").to_dict(),
                    ]
                )
                webui_audit_log.record(
                    action="remote_skill_source_add",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=_source_descriptor(entry),
                    detail={
                        "changed": changed,
                        "code": report["code"],
                        "persisted_fields": persisted_fields,
                        "failed_field": field_name,
                        "trace_id": report["trace_id"],
                    },
                    outcome="error",
                )
                raise HTTPException(status_code=500, detail=report) from exc

        approved_count = 0
        if auto_approve:
            try:
                prepared_sources = await remote_skill_review.prepare_remote_skill_reviews(
                    getattr(cfg, "personification_skill_sources", None),
                    cfg,
                    logger,
                    data_dir=get_data_dir(cfg),
                )
                reviews = remote_skill_review.list_remote_skill_reviews(
                    prepared_sources,
                    logger,
                )
                review_key = ""
                for item in reviews:
                    if _source_dedupe_key(item) == dedupe_key:
                        review_key = str(item.get("key") or "")
                        break
                if review_key:
                    approved_count, _items = remote_skill_review.review_remote_skill_sources(
                        prepared_sources,
                        logger,
                        selector=review_key,
                        status="approved",
                        operator=admin.qq,
                    )
            except Exception as exc:
                operation_steps.extend(
                    (
                        step("auto_approve", "自动批准远程来源", "unknown", "审核写入的最终状态未知。"),
                        step("reload_runtime", "重载 Skill runtime", "skipped", "审核状态需要先核对。"),
                    )
                )
                report = _exception_report(
                    exc,
                    code="remote_skill_source_auto_approve_unknown",
                    phase="review_persistence",
                    title="远程来源已保存，但自动批准状态未知",
                    message="来源配置已完整持久化，自动批准阶段异常中断。",
                    suggestion="刷新远程来源列表核对审核状态；确认前不要重复批准或 reload。",
                    steps=tuple(operation_steps),
                    retryable=False,
                    partial=True,
                    outcome_unknown=True,
                )
                report["details"].extend(
                    [
                        detail("来源类型", _source_descriptor(entry)).to_dict(),
                        detail("已持久化字段", persisted_fields, "ok").to_dict(),
                    ]
                )
                webui_audit_log.record(
                    action="remote_skill_source_add",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=_source_descriptor(entry),
                    detail={"changed": changed, "code": report["code"], "trace_id": report["trace_id"]},
                    outcome="unknown",
                )
                raise HTTPException(status_code=500, detail=report) from exc
            operation_steps.append(
                step(
                    "auto_approve",
                    "自动批准远程来源",
                    "ok" if approved_count > 0 else "warn",
                    "审核状态已更新为 approved。" if approved_count > 0 else "没有确认到可批准的审核 key。",
                )
            )
        else:
            operation_steps.append(
                step("auto_approve", "自动批准远程来源", "skipped", "请求未启用 auto_approve。")
            )
        operation_steps.append(
            step("reload_runtime", "重载 Skill runtime", "pending", "来源与审核变化尚未应用到 tool registry。")
        )

        webui_audit_log.record(
            action="remote_skill_source_add",
            qq=admin.qq,
            device_id=admin.device_id,
            target=_source_descriptor(entry),
            detail={
                "changed": changed,
                "duplicate": duplicate,
                "auto_approve": auto_approve,
                "approved_count": approved_count,
            },
        )
        if duplicate and not changed:
            code, title = "remote_skill_source_duplicate", "远程 Skill 来源已存在"
        elif duplicate:
            code, title = "remote_skill_source_updated", "远程 Skill 来源已更新"
        elif approved_count > 0:
            code, title = "remote_skill_source_added_approved", "远程 Skill 来源已添加并批准"
        else:
            code, title = "remote_skill_source_added", "远程 Skill 来源已添加"
        warnings = ()
        if auto_approve and approved_count <= 0:
            warnings = ("来源已保存，但自动批准未确认成功；请在来源列表中人工核对。",)
        report = diagnostic(
            ok=True,
            code=code,
            phase="review_complete" if approved_count > 0 else "config_persisted",
            title=title,
            message=(
                "相同来源未重复添加；配置与当前审核状态已保留。"
                if duplicate and not changed
                else "来源配置已持久化，runtime reload 后才会更新工具注册。"
            ),
            details=(
                detail("来源类型", _source_descriptor(entry)),
                detail("来源变更", "updated" if changed else "unchanged", "ok" if changed else "warn"),
                detail("自动批准", approved_count > 0, "ok" if approved_count > 0 else "info"),
                detail("已持久化字段", persisted_fields, "ok"),
            ),
            steps=tuple(operation_steps),
            warnings=warnings,
            suggestion="执行一次 Skill runtime reload，使来源与审核状态应用到 tool registry。",
        )
        return _attach_diagnostic(
            {
                "success": True,
                "changed": changed,
                "source": entry,
                "auto_approved": approved_count > 0,
                "needs_reload": True,
                "config_results": config_results,
            },
            report,
        )

    @router.post("/reload")
    async def reload_skills(admin: AdminIdentity = Depends(require_admin)) -> dict:
        bundle = getattr(runtime, "runtime_bundle", None)
        reload_services = getattr(bundle, "reload_all_runtime_services", None)
        if not callable(reload_services):
            reload_services = getattr(bundle, "reload_runtime_services", None)
        if not callable(reload_services):
            raise HTTPException(
                status_code=503,
                detail=diagnostic(
                    ok=False,
                    code="skill_runtime_reload_unavailable",
                    phase="reload_preflight",
                    title="当前 runtime 不支持 Skill 重载",
                    message="runtime bundle 未提供 reload_runtime_services。",
                    steps=(step("reload_preflight", "检查 runtime reload 能力", "error", "没有可调用的重载器。"),),
                    suggestion="通过服务器部署流程重启 Bot，使 Skill 配置生效。",
                    retryable=False,
                ),
            )
        before_registry = _tool_registry(runtime)
        before_count = len(before_registry.all()) if before_registry is not None else 0
        try:
            reload_result = reload_services()
            if inspect.isawaitable(reload_result):
                await reload_result
        except Exception as exc:
            report = _exception_report(
                exc,
                code="skill_runtime_reload_outcome_unknown",
                phase="runtime_reload",
                title="Skill runtime 重载状态未知",
                message="重载流程异常中断，部分 runtime service 可能已经替换。",
                suggestion="先刷新 Skill 列表并检查脱敏日志；确认 registry 状态前不要重复 reload。",
                steps=(
                    step("reload_preflight", "检查 runtime reload 能力", "ok", "已找到可调用的重载器。"),
                    step("reload_runtime", "重建 runtime services", "unknown", "无法确认所有 runtime service 的最终状态。"),
                    step("verify_registry", "验证 tool registry", "skipped", "重载异常后未取得可信快照。"),
                ),
                retryable=False,
                partial=True,
                outcome_unknown=True,
            )
            webui_audit_log.record(
                action="skill_runtime_reload",
                qq=admin.qq,
                device_id=admin.device_id,
                detail={"code": report["code"], "trace_id": report["trace_id"]},
                outcome="unknown",
            )
            logger = _logger(runtime)
            logger.warning(f"[webui] Skill runtime reload interrupted: {type(exc).__name__} trace={report['trace_id']}")
            raise HTTPException(status_code=500, detail=report) from exc
        after_registry = _tool_registry(runtime)
        after_count = len(after_registry.all()) if after_registry is not None else 0
        mcp_count = len(_mcp_tools_payload())
        webui_audit_log.record(
            action="skill_runtime_reload",
            qq=admin.qq,
            device_id=admin.device_id,
            detail={"before_tools": before_count, "after_tools": after_count, "mcp_tools": mcp_count},
        )
        report = diagnostic(
            ok=True,
            code="skill_runtime_reloaded",
            phase="reload_complete",
            title="Skill runtime 已重载",
            message="runtime services 已重建，并重新读取 tool registry 与 MCP 注册概览。",
            details=(
                detail("工具数量（重载前）", before_count),
                detail("工具数量（重载后）", after_count, "ok"),
                detail("MCP stdio 工具", mcp_count, "ok"),
            ),
            steps=(
                step("reload_preflight", "检查 runtime reload 能力", "ok", "已找到可调用的重载器。"),
                step("reload_runtime", "重建 runtime services", "ok", "reload_runtime_services 已完成。"),
                step("verify_registry", "验证 tool registry", "ok", "已重新读取 Skill 与 MCP 工具数量。"),
            ),
            suggestion="检查远程来源审核状态与工具列表，确认预期 Skill 已注册。",
        )
        return _attach_diagnostic({"success": True}, report)

    return router
