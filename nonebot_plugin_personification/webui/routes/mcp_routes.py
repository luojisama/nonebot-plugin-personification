from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from ...core import webui_audit_log
from ...core.mcp_management import (
    get_mcp_manager,
    mcp_registry_sources,
    resolve_registry_source,
)
from ...core.mcp_builtin import BUILTIN_SOCIAL_MCP_ID
from ...core.mcp_builtin_platform_store import BuiltinPlatformStore, CONFIG_FIELDS, PLATFORMS
from ...core.meme_learning_store import LearningThresholds, MemeLearningStore
from ...core.slang_learning import SlangLearningPipeline
from ...core.operation_diagnostics import diagnostic, detail, step
from ..deps import AdminIdentity, get_client_ip, require_admin


_PLATFORM_LABELS = {"bilibili": "B站", "douyin": "抖音", "tieba": "贴吧", "xiaoheihe": "小黑盒"}
_COVER_HOSTS = {
    "bilibili": ("hdslb.com", "biliimg.com"),
    "douyin": ("douyinpic.com", "byteimg.com", "pstatp.com"),
    "tieba": ("tiebapic.baidu.com", "imgsa.baidu.com", "hiphotos.baidu.com"),
    "xiaoheihe": ("xiaoheihe.cn", "heybox.cn", "max-c.com"),
}


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _auth_owner(admin: AdminIdentity, platform: str) -> str:
    return f"{admin.qq}:{admin.device_id}:{platform}"


def _safe_builtin_error(exc: Exception, title: str) -> HTTPException:
    code = str(exc).strip()
    status = 409 if code in {"revision_conflict", "builtin MCP is disabled"} else 404 if isinstance(exc, KeyError) else 400 if isinstance(exc, (TypeError, ValueError)) else 503
    messages = {
        "revision_conflict": "配置已被其他管理员更新，请刷新页面后重试。",
        "builtin MCP is disabled": "请先开启原生社交平台 MCP 服务。",
        "config contains unsupported fields": "平台配置混入了不受支持的字段，请刷新页面后重试。",
    }
    return HTTPException(status_code=status, detail={
        "ok": False,
        "code": code if code in messages else "builtin_social_operation_failed",
        "title": title,
        "message": messages.get(code, "原生社交平台 MCP 操作未完成，请检查服务、登录态或平台风控状态。"),
        "error_type": type(exc).__name__,
    })


def _runtime_tool_caller(runtime: Any) -> Any:
    bundle = getattr(runtime, "runtime_bundle", None)
    deps = getattr(bundle, "reply_processor_deps", None)
    inner = getattr(deps, "runtime", None)
    return getattr(inner, "lite_tool_caller", None) or getattr(inner, "agent_tool_caller", None)


def _cover_url_allowed(platform: str, value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and any(host == suffix or host.endswith("." + suffix) for suffix in _COVER_HOSTS.get(platform, ()))
    )


def _failure(
    exc: Exception,
    title: str,
    *,
    validation: bool = False,
    message: str = "",
) -> HTTPException:
    status = 400 if validation or isinstance(exc, ValueError) else 404 if isinstance(exc, KeyError) else 500
    safe_message = message or (
        "请求字段不符合 MCP 操作要求。"
        if status == 400
        else "找不到指定的 MCP 安装或工具。"
        if status == 404
        else "MCP 操作未完成；第三方进程、Registry 或 runtime 返回异常。"
    )
    return HTTPException(
        status_code=status,
        detail=diagnostic(
            ok=False,
            code="mcp_request_invalid" if status == 400 else "mcp_installation_not_found" if status == 404 else "mcp_operation_failed",
            phase="validation" if status == 400 else "lookup" if status == 404 else "runtime",
            title=title,
            message=safe_message,
            details=(detail("失败类型", type(exc).__name__, "error"),),
            steps=(step("mcp_operation", title, "error", "操作未完成。"),),
            suggestion="检查输入和当前安装状态后重试。" if status < 500 else "查看脱敏日志；若预检已执行，先刷新安装列表确认是否已落库。",
            retryable=status != 404,
        ),
    )


def _strict_bool(body: dict[str, Any], key: str, *, title: str, default: Any = None) -> bool:
    value = body.get(key, default)
    if type(value) is not bool:
        raise _failure(
            ValueError(key),
            title,
            validation=True,
            message=f"{key} 必须是 JSON boolean。",
        )
    return value


def build_mcp_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/mcp", tags=["mcp"])
    platform_store = BuiltinPlatformStore()

    def learning_store() -> MemeLearningStore:
        cfg = runtime.plugin_config
        return MemeLearningStore(
            LearningThresholds(
                auto_understand_min_sources=getattr(cfg, "personification_auto_understand_min_sources", 2),
                auto_use_min_sources=getattr(cfg, "personification_auto_use_min_sources", 3),
                auto_use_min_platforms=getattr(cfg, "personification_auto_use_min_platforms", 2),
                claim_min_confidence=getattr(cfg, "personification_claim_min_confidence", 0.72),
                semantic_equivalence_min_confidence=getattr(
                    cfg,
                    "personification_semantic_equivalence_min_confidence",
                    0.80,
                ),
                reverify_after_days=getattr(cfg, "personification_reverify_after_days", 30),
                stale_after_days=getattr(cfg, "personification_stale_after_days", 90),
            )
        )

    def manager():
        return get_mcp_manager(runtime)

    async def run_slang_research(body: dict[str, Any]) -> dict[str, Any]:
        term = " ".join(str(body.get("term") or body.get("query") or "").split())[:100]
        context = " ".join(str(body.get("context") or term).split())[:1000]
        game = " ".join(str(body.get("game") or "").split())[:100]
        depth = str(body.get("depth") or "auto")
        tool_name = str(body.get("tool") or "research_game_slang").strip()
        if tool_name not in {"social_content_search", "research_game_slang"}:
            raise ValueError("unsupported preview tool")
        if not term or depth not in {"auto", "deep"}:
            raise ValueError("term and valid depth are required")
        try:
            limit = max(1, min(50, int(body.get("limit", 10) or 10)))
        except (TypeError, ValueError) as exc:
            raise ValueError("limit must be an integer") from exc
        if tool_name == "social_content_search":
            query = " ".join(str(body.get("query") or " ".join((game, term))).split())[:200]
            tool_arguments: dict[str, Any] = {"query": query, "limit": limit}
        else:
            tool_arguments = {
                "term": term,
                "context": context,
                "game": game,
                "depth": depth,
                "limit": limit,
            }
        raw = await manager().builtin_call_tool(
            tool_name,
            tool_arguments,
        )
        try:
            packet = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("invalid builtin content packet") from exc
        caller = _runtime_tool_caller(runtime)
        claims: list[dict[str, Any]] = []
        senses: list[dict[str, Any]] = []
        if caller is not None and tool_name == "research_game_slang":
            pipeline = SlangLearningPipeline(
                tool_caller=caller,
                max_claims=max(
                    1,
                    min(
                        50,
                        int(
                            body.get(
                                "max_claims",
                                getattr(runtime.plugin_config, "personification_slang_max_claims", 20),
                            )
                            or 20
                        ),
                    ),
                ),
            )
            claims = await pipeline.extract_claims(packet, target_term=term)
            senses = await learning_store().ingest_claims(
                claims,
                semantic_pipeline=pipeline,
                model_route="webui_social_research",
            )
        return {
            "tool_name": tool_name,
            "packet": packet,
            "claims": claims,
            "senses": senses,
            "target_term": term,
            "delivery": {
                "evidence_delivery": "not_applicable",
                "visible_output_recovered": False,
                "outbound_delivery": "not_applicable",
                "note": "工具预览不生成最终回复，也不发送 QQ 消息",
            },
        }

    @router.get("/sources")
    async def sources(_: AdminIdentity = Depends(require_admin)) -> dict:
        return {"sources": mcp_registry_sources(runtime.plugin_config)}

    @router.get("/search")
    async def search(
        source_id: str = "official",
        q: str = "",
        cursor: str = "",
        limit: int = Query(20, ge=1, le=50),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            source = resolve_registry_source(runtime.plugin_config, source_id)
            return await manager().registry_client.search(source, q, limit=limit, cursor=cursor)
        except Exception as exc:
            raise _failure(exc, "MCP Registry 搜索失败") from exc

    @router.get("/detail")
    async def registry_detail(
        name: str,
        source_id: str = "official",
        fresh: bool = False,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict:
        try:
            source = resolve_registry_source(runtime.plugin_config, source_id)
            result = await manager().registry_client.detail(source, name, fresh=fresh)
            result.pop("raw", None)
            return result
        except Exception as exc:
            raise _failure(exc, "MCP Server 详情读取失败") from exc

    @router.get("/installations")
    async def installations(_: AdminIdentity = Depends(require_admin)) -> dict:
        current_manager = manager()
        await current_manager.refresh_process_states()
        items = current_manager.list_public()
        return {"installations": items, "total": len(items)}

    @router.post("/install")
    async def install(
        request: Request,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        if body.get("confirm_execution") is not True:
            raise _failure(
                ValueError("confirm_execution"),
                "MCP 安装未确认",
                validation=True,
                message="必须以 JSON boolean true 确认下载并执行第三方 package。",
            )
        fresh_fetch = _strict_bool(body, "fresh_fetch", title="MCP 安装请求无效", default=True)
        source_id = str(body.get("source_id") or "official")
        server_name = str(body.get("server_name") or "")
        try:
            source = resolve_registry_source(runtime.plugin_config, source_id)
            server_detail = await manager().registry_client.detail(source, server_name, fresh=fresh_fetch)
            item = await manager().install(
                source=source,
                detail=server_detail,
                package_index=int(body.get("package_index") or 0),
                package_digest=str(body.get("package_digest") or ""),
                inputs=body.get("inputs") if isinstance(body.get("inputs"), dict) else {},
                prefix=str(body.get("name_prefix") or ""),
                creator=admin.qq,
            )
        except Exception as exc:
            webui_audit_log.record(
                action="mcp_install",
                qq=admin.qq,
                device_id=admin.device_id,
                target=server_name,
                ip_hash=get_client_ip(request),
                detail={"source_id": source_id, "error_type": type(exc).__name__},
                outcome="error",
            )
            raise _failure(exc, "MCP Server 安装失败") from exc
        enabled = sum(1 for tool in item.get("tools", []) if tool.get("enabled"))
        disabled = len(item.get("tools", [])) - enabled
        webui_audit_log.record(
            action="mcp_install",
            qq=admin.qq,
            device_id=admin.device_id,
            target=item["installation_id"],
            ip_hash=get_client_ip(request),
            detail={"source_id": source_id, "server_name": server_name, "enabled_tools": enabled, "disabled_tools": disabled},
        )
        report = diagnostic(
            ok=True,
            code="mcp_installed",
            phase="ready",
            title="MCP Server 已安装",
            message="package 已按精确版本预检；所有工具默认关闭，需管理员逐项确认风险后启用。",
            details=(detail("已启用工具", enabled, "ok"), detail("待人工确认工具", disabled, "warn" if disabled else "ok")),
            steps=(
                step("resolve", "读取权威 metadata", "ok", "已从配置的 Registry 重新读取详情。"),
                step("preflight", "启动并读取 tools/list", "ok", "第三方 package 已完成显式确认后的预检。"),
                step("persist", "保存安装与 Secret 引用", "ok", "Secret 未写入数据库、审计或响应。"),
                step("activate", "等待逐工具授权", "skipped", "未信任 publisher annotation；当前没有自动注册工具。"),
            ),
            warnings=("Registry 仅验证发布者命名空间，不代表 package 经过安全审计。",),
            operation_id=item["installation_id"],
        )
        return {"installation": item, "diagnostic": report}

    @router.post("/installations/{installation_id}/toggle")
    async def toggle_installation(
        installation_id: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        enabled = _strict_bool(body, "enabled", title="MCP Server 状态请求无效")
        try:
            item = await manager().toggle_installation(installation_id, enabled)
        except Exception as exc:
            current = manager().public_installation(installation_id)
            if current is not None and current.get("desired_enabled") is enabled:
                webui_audit_log.record(
                    action="mcp_toggle",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=installation_id,
                    detail={"enabled": enabled, "runtime_error_type": type(exc).__name__},
                    outcome="partial",
                )
                report = diagnostic(
                    ok=False,
                    code="mcp_server_start_partial" if enabled else "mcp_server_stop_partial",
                    phase="activation" if enabled else "shutdown",
                    title="MCP Server 状态已保存，但 runtime 未完成",
                    message="desired 状态已持久化，process 未达到请求状态。",
                    details=(detail("失败类型", type(exc).__name__, "error"), detail("desired_enabled", enabled, "ok")),
                    steps=(
                        step("validate", "校验请求", "ok"),
                        step("persist", "持久化 desired 状态", "ok"),
                        step("runtime", "同步 catalog 与 process", "error", "runtime 未完成请求状态。"),
                    ),
                    suggestion="保留当前 desired 状态；修复 package 或环境后执行 MCP reload。",
                    retryable=True,
                    partial=True,
                    operation_id=installation_id,
                )
                raise HTTPException(status_code=500, detail=report) from exc
            raise _failure(exc, "MCP Server 状态切换失败") from exc
        webui_audit_log.record(action="mcp_toggle", qq=admin.qq, device_id=admin.device_id, target=installation_id, detail={"enabled": enabled})
        report = diagnostic(
            ok=True,
            code="mcp_server_toggled",
            phase="runtime",
            title="MCP Server 状态已更新",
            message="Server 已按 desired 状态完成 catalog 同步与 process 调整。",
            details=(
                detail("desired_enabled", item.get("desired_enabled"), "ok"),
                detail("process_state", item.get("process_state"), "ok"),
                detail("保留授权工具", item.get("authorized_count", 0), "info"),
            ),
            steps=(
                step("persist", "持久化 desired 状态", "ok"),
                step("catalog", "同步 tool catalog", "ok" if enabled else "skipped"),
                step("runtime", "调整 process 与注册工具", "ok"),
            ),
            operation_id=installation_id,
        )
        return {"installation": item, "diagnostic": report}

    @router.post("/installations/{installation_id}/tools/{remote_name:path}/toggle")
    async def toggle_tool(
        installation_id: str,
        remote_name: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        enabled = _strict_bool(body, "enabled", title="MCP 工具状态请求无效")
        confirm_side_effect = _strict_bool(
            body,
            "confirm_side_effect",
            title="MCP 工具状态请求无效",
            default=False,
        )
        current = manager().public_installation(installation_id)
        if current is None:
            raise _failure(KeyError(installation_id), "找不到 MCP 安装")
        policy = next((tool for tool in current.get("tools", []) if tool.get("remote_name") == remote_name), None)
        if policy is None:
            raise _failure(KeyError(remote_name), "找不到 MCP 工具")
        if enabled and not confirm_side_effect:
            raise _failure(
                ValueError("confirm_side_effect"),
                "MCP 工具启用未确认",
                validation=True,
                message="MCP 工具需要以 JSON boolean true 显式确认风险后才能启用。",
            )
        try:
            item = await manager().toggle_tool(
                installation_id,
                remote_name,
                enabled,
                approve_side_effect=confirm_side_effect,
            )
        except Exception as exc:
            current = manager().public_installation(installation_id)
            current_policy = next(
                (tool for tool in (current or {}).get("tools", []) if tool.get("remote_name") == remote_name),
                None,
            )
            if current_policy is not None and current_policy.get("authorized") is enabled:
                webui_audit_log.record(
                    action="mcp_tool_toggle",
                    qq=admin.qq,
                    device_id=admin.device_id,
                    target=installation_id,
                    detail={
                        "remote_name": remote_name,
                        "enabled": enabled,
                        "confirmed_side_effect": confirm_side_effect,
                        "runtime_error_type": type(exc).__name__,
                    },
                    outcome="partial",
                )
                report = diagnostic(
                    ok=False,
                    code="mcp_tool_toggle_partial",
                    phase="activation",
                    title="MCP 工具授权已保存，但 runtime 未完成",
                    message="工具授权状态已持久化，process 或注册状态未完成同步。",
                    details=(detail("失败类型", type(exc).__name__, "error"), detail("authorized", enabled, "ok")),
                    steps=(
                        step("persist", "持久化工具授权", "ok"),
                        step("catalog", "同步 tool catalog", "error"),
                        step("runtime", "更新工具注册", "error"),
                    ),
                    suggestion="保留当前授权；修复 Server 后执行 MCP reload。",
                    retryable=True,
                    partial=True,
                    operation_id=installation_id,
                )
                raise HTTPException(status_code=500, detail=report) from exc
            raise _failure(exc, "MCP 工具状态切换失败") from exc
        webui_audit_log.record(action="mcp_tool_toggle", qq=admin.qq, device_id=admin.device_id, target=installation_id, detail={"remote_name": remote_name, "enabled": enabled, "confirmed_side_effect": confirm_side_effect})
        report = diagnostic(
            ok=True,
            code="mcp_tool_toggled",
            phase="runtime",
            title="MCP 工具授权已更新",
            message="授权、catalog 与当前注册状态已完成同步。",
            details=(detail("remote_name", remote_name, "info"), detail("authorized", enabled, "ok")),
            steps=(
                step("persist", "持久化工具授权", "ok"),
                step("catalog", "重新读取 tool catalog", "ok" if item.get("desired_enabled") else "skipped"),
                step("runtime", "更新工具注册", "ok"),
            ),
            operation_id=installation_id,
        )
        return {"installation": item, "diagnostic": report}

    @router.delete("/installations/{installation_id}")
    async def delete_installation(
        installation_id: str,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict:
        if body.get("confirm") != "delete":
            raise _failure(ValueError("删除 MCP 安装需要 confirm=delete"), "MCP 删除未确认", validation=True)
        try:
            await manager().delete(installation_id)
        except Exception as exc:
            raise _failure(exc, "MCP Server 删除失败") from exc
        webui_audit_log.record(action="mcp_delete", qq=admin.qq, device_id=admin.device_id, target=installation_id)
        return {"success": True, "installation_id": installation_id}

    @router.post("/reload")
    async def reload_mcp(admin: AdminIdentity = Depends(require_admin)) -> dict:
        try:
            result = await manager().reload()
        except Exception as exc:
            raise _failure(exc, "MCP reload 失败") from exc
        webui_audit_log.record(action="mcp_reload", qq=admin.qq, device_id=admin.device_id, detail=result, outcome="partial" if result["failed"] else "ok")
        failed = int(result.get("failed") or 0)
        report = diagnostic(
            ok=failed == 0,
            code="mcp_reload_complete" if failed == 0 else "mcp_reload_partial",
            phase="runtime_reload",
            title="MCP reload 已完成" if failed == 0 else "MCP reload 部分完成",
            message="已逐个同步启用安装的 catalog 与 process 状态。",
            details=(
                detail("running", result.get("running", 0), "ok"),
                detail("ready", result.get("ready", 0), "info"),
                detail("failed", failed, "error" if failed else "ok"),
                detail("catalog_added", result.get("catalog_added", 0), "info"),
                detail("catalog_updated", result.get("catalog_updated", 0), "info"),
                detail("catalog_removed", result.get("catalog_removed", 0), "info"),
            ),
            steps=(
                step("stop", "停止旧 managed process", "ok"),
                step("catalog", "重新同步 tool catalog", "warn" if failed else "ok"),
                step("restore", "恢复授权工具注册", "warn" if failed else "ok"),
            ),
            suggestion="检查失败安装的 last_error 后再次 reload。" if failed else "",
            retryable=failed > 0,
            partial=failed > 0,
        )
        return {**result, "diagnostic": report}

    @router.get("/builtin/social-research/status")
    async def builtin_social_status(
        response: Response,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        installation = manager().public_installation(BUILTIN_SOCIAL_MCP_ID)
        persisted = {item["platform"]: item for item in platform_store.list()}
        runtime_status: dict[str, Any] = {"schema_version": 1, "platforms": {}}
        if installation and installation.get("desired_enabled"):
            try:
                runtime_status = await manager().builtin_request("personification/builtin/status", {})
            except Exception:
                runtime_status = {"schema_version": 1, "platforms": {}, "state": "unavailable"}
        platforms = {}
        for platform in PLATFORMS:
            stored = persisted.get(platform, {"platform": platform, "enabled": False, "revision": 0, "config": {}})
            live = dict((runtime_status.get("platforms") or {}).get(platform) or {})
            merged_config = {**dict(live.get("config") or {}), **dict(stored.get("config") or {})}
            platforms[platform] = {
                **live,
                **stored,
                # Older/restarting native processes may still report the
                # top-level ``enabled`` flag inside config. Never reflect an
                # unknown field back to the browser's editable config model.
                "config": {key: value for key, value in merged_config.items() if key in CONFIG_FIELDS},
                "runtime_state": live.get("state", "service_disabled"),
            }
        return {"installation": installation, "platforms": platforms}

    @router.post("/builtin/social-research/platforms/{platform}/configure")
    async def configure_builtin_platform(
        platform: str,
        request: Request,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        try:
            if type(body.get("revision")) is not int or not isinstance(body.get("config"), dict):
                raise ValueError("revision must be an integer and config must be an object")
            stored = platform_store.update(
                platform=platform,
                enabled=_strict_bool(body, "enabled", title="平台配置无效"),
                config=body.get("config") if isinstance(body.get("config"), dict) else {},
                expected_revision=int(body.get("revision")),
            )
            live = await manager().builtin_request(
                "personification/builtin/configure",
                {"platform": platform, "enabled": stored["enabled"], "config": stored["config"]},
            )
        except Exception as exc:
            webui_audit_log.record(
                action="mcp_builtin_platform_configure", qq=admin.qq, device_id=admin.device_id,
                target=platform, ip_hash=get_client_ip(request), detail={"error_type": type(exc).__name__}, outcome="error",
            )
            raise _safe_builtin_error(exc, "平台配置未完成") from exc
        webui_audit_log.record(
            action="mcp_builtin_platform_configure", qq=admin.qq, device_id=admin.device_id,
            target=platform, ip_hash=get_client_ip(request), detail={"enabled": stored["enabled"], "revision": stored["revision"]},
        )
        return {"platform": stored, "runtime": live}

    @router.post("/builtin/social-research/auth/start")
    async def builtin_auth_start(
        request: Request,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        platform = str(body.get("platform") or "")
        mode = str(body.get("mode") or "embedded_qr")
        if platform not in PLATFORMS or mode not in {"embedded_qr", "manual_browser"}:
            raise _safe_builtin_error(ValueError("unsupported platform"), "登录请求无效")
        try:
            result = await manager().builtin_request(
                "personification/builtin/auth/start",
                {"platform": platform, "owner": _auth_owner(admin, platform), "mode": mode},
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "登录会话创建失败") from exc
        webui_audit_log.record(
            action="mcp_builtin_auth_start", qq=admin.qq, device_id=admin.device_id,
            target=platform, ip_hash=get_client_ip(request), detail={"mode": mode},
        )
        return result

    @router.get("/builtin/social-research/auth/{session_id}/status")
    async def builtin_auth_status(
        session_id: str,
        platform: str,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        if platform not in PLATFORMS:
            raise _safe_builtin_error(ValueError("unsupported platform"), "登录状态请求无效")
        try:
            return await manager().builtin_request(
                "personification/builtin/auth/status",
                {"session_id": session_id, "owner": _auth_owner(admin, platform)},
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "登录状态读取失败") from exc

    @router.get("/builtin/social-research/auth/{session_id}/qrcode")
    async def builtin_auth_qrcode(
        session_id: str,
        platform: str,
        admin: AdminIdentity = Depends(require_admin),
    ) -> Response:
        if platform not in PLATFORMS:
            raise _safe_builtin_error(ValueError("unsupported platform"), "二维码请求无效")
        try:
            result = await manager().builtin_request(
                "personification/builtin/auth/qrcode",
                {"session_id": session_id, "owner": _auth_owner(admin, platform)},
            )
            image = base64.b64decode(str(result.get("data_base64") or ""), validate=True)
        except Exception as exc:
            raise _safe_builtin_error(exc, "登录二维码读取失败") from exc
        return Response(
            content=image,
            media_type="image/png",
            headers={"Cache-Control": "no-store, private", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @router.post("/builtin/social-research/auth/{session_id}/cancel")
    async def builtin_auth_cancel(
        session_id: str,
        platform: str,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        if platform not in PLATFORMS:
            raise _safe_builtin_error(ValueError("unsupported platform"), "登录会话取消失败")
        try:
            return await manager().builtin_request(
                "personification/builtin/auth/cancel",
                {"session_id": session_id, "owner": _auth_owner(admin, platform)},
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "登录会话取消失败") from exc

    @router.post("/builtin/social-research/auth/logout")
    async def builtin_auth_logout(
        request: Request,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        platform = str(body.get("platform") or "")
        expected = f"确认注销{_PLATFORM_LABELS.get(platform, platform)}"
        if platform not in PLATFORMS or body.get("confirm") != expected:
            raise _safe_builtin_error(ValueError("logout confirmation mismatch"), f"请输入精确确认文本：{expected}")
        try:
            result = await manager().builtin_request("personification/builtin/auth/logout", {"platform": platform})
        except Exception as exc:
            raise _safe_builtin_error(exc, "平台注销失败") from exc
        webui_audit_log.record(
            action="mcp_builtin_auth_logout", qq=admin.qq, device_id=admin.device_id,
            target=platform, ip_hash=get_client_ip(request), detail={"profile_deleted": True},
        )
        return result

    @router.post("/builtin/social-research/preview")
    async def builtin_social_preview(
        request: Request,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        try:
            result = await run_slang_research(body)
        except Exception as exc:
            raise _safe_builtin_error(exc, "社交平台检索预览失败") from exc
        webui_audit_log.record(
            action="mcp_builtin_social_preview", qq=admin.qq, device_id=admin.device_id,
            target=str(result.get("target_term") or "")[:100], ip_hash=get_client_ip(request),
            detail={"claims": len(result.get("claims") or []), "senses": len(result.get("senses") or [])},
        )
        return result

    @router.get("/builtin/social-research/cover/{cover_ref}")
    async def builtin_cover_proxy(
        cover_ref: str,
        _: AdminIdentity = Depends(require_admin),
    ) -> Response:
        if not re.fullmatch(r"cover_[0-9a-f]{40}", cover_ref):
            raise HTTPException(status_code=404, detail="封面引用不存在。")
        try:
            resolved = await manager().builtin_request(
                "personification/builtin/cover/resolve", {"cover_ref": cover_ref}
            )
            platform = str(resolved.get("platform") or "")
            url = str(resolved.get("url") or "")
            if not _cover_url_allowed(platform, url):
                raise ValueError("cover URL rejected")
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                for _redirect in range(4):
                    async with client.stream("GET", url, headers={"Accept": "image/*"}) as upstream:
                        if upstream.status_code in {301, 302, 303, 307, 308}:
                            url = urljoin(url, upstream.headers.get("location", ""))
                            if not _cover_url_allowed(platform, url):
                                raise ValueError("cover redirect rejected")
                            continue
                        upstream.raise_for_status()
                        content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        content_length = int(upstream.headers.get("content-length", "0") or 0)
                        if not content_type.startswith("image/") or content_length > 5 * 1024 * 1024:
                            raise ValueError("cover response rejected")
                        content = bytearray()
                        async for chunk in upstream.aiter_bytes():
                            content.extend(chunk)
                            if len(content) > 5 * 1024 * 1024:
                                raise ValueError("cover response rejected")
                        return Response(
                            content=bytes(content),
                            media_type=content_type,
                            headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
                        )
                raise ValueError("too many cover redirects")
        except Exception as exc:
            raise _safe_builtin_error(exc, "封面代理读取失败") from exc

    @router.get("/builtin/social-research/slang/senses")
    async def list_slang_senses(
        response: Response,
        status: str = "",
        term: str = "",
        limit: int = Query(200, ge=1, le=2000),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        try:
            items = learning_store().list_senses(status=status, term=term, limit=limit)
        except Exception as exc:
            raise _safe_builtin_error(exc, "学习词条读取失败") from exc
        return {"senses": items, "total": len(items)}

    @router.get("/builtin/social-research/slang/senses/{sense_id}")
    async def slang_sense_detail(
        sense_id: str,
        response: Response,
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        item = learning_store().get_sense(sense_id, include_detail=True)
        if item is None:
            raise _safe_builtin_error(KeyError(sense_id), "找不到该 sense")
        return {"sense": item}

    async def update_manual_sense(
        *,
        sense_id: str,
        status: str,
        body: dict[str, Any],
        admin: AdminIdentity,
        action: str,
    ) -> dict[str, Any]:
        try:
            item = learning_store().set_manual_status(
                sense_id,
                status=status,
                actor=admin.qq,
                expected_revision=int(body.get("revision")),
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "Sense 状态更新失败") from exc
        webui_audit_log.record(
            action=action, qq=admin.qq, device_id=admin.device_id, target=sense_id,
            detail={"status": status, "revision": item.get("revision")},
        )
        return {"sense": item}

    @router.post("/builtin/social-research/slang/senses/{sense_id}/accept")
    async def accept_slang_sense(
        sense_id: str,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        return await update_manual_sense(sense_id=sense_id, status="manual_locked", body=body, admin=admin, action="meme_sense_accept")

    @router.post("/builtin/social-research/slang/senses/{sense_id}/reject")
    async def reject_slang_sense(
        sense_id: str,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        return await update_manual_sense(sense_id=sense_id, status="rejected", body=body, admin=admin, action="meme_sense_reject")

    @router.post("/builtin/social-research/slang/senses/{sense_id}/lock")
    async def lock_slang_sense(
        sense_id: str,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        return await update_manual_sense(sense_id=sense_id, status="manual_locked", body=body, admin=admin, action="meme_sense_lock")

    @router.post("/builtin/social-research/slang/senses/{sense_id}/reverify")
    async def reverify_slang_sense(
        sense_id: str,
        request: Request,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        current = learning_store().get_sense(sense_id)
        if current is None:
            raise _safe_builtin_error(KeyError(sense_id), "找不到该 sense")
        if current["revision"] != int(body.get("revision", -1)):
            raise _safe_builtin_error(RuntimeError("revision_conflict"), "Sense 已更新")
        try:
            research = await run_slang_research({
                "term": current["term"],
                "context": current["usage_context"] or current["meaning"],
                "game": (current.get("game_context") or {}).get("canonical_name", ""),
                "depth": "deep",
                "max_claims": body.get(
                    "max_claims",
                    getattr(runtime.plugin_config, "personification_slang_max_claims", 20),
                ),
            })
        except Exception as exc:
            raise _safe_builtin_error(exc, "重新验证失败") from exc
        webui_audit_log.record(
            action="meme_sense_reverify", qq=admin.qq, device_id=admin.device_id, target=sense_id,
            ip_hash=get_client_ip(request), detail={"claims": len(research.get("claims") or [])},
        )
        return {"sense": learning_store().get_sense(sense_id, include_detail=True), "research": research}

    @router.post("/builtin/social-research/slang/senses/merge")
    async def merge_slang_senses(
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        try:
            item = learning_store().merge_senses(
                target_sense_id=str(body.get("target_sense_id") or ""),
                source_sense_ids=[str(value) for value in list(body.get("source_sense_ids") or [])],
                expected_revisions={str(key): int(value) for key, value in dict(body.get("revisions") or {}).items()},
                actor=admin.qq,
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "Sense 合并失败") from exc
        webui_audit_log.record(
            action="meme_sense_merge", qq=admin.qq, device_id=admin.device_id,
            target=item["sense_id"], detail={"source_sense_ids": list(body.get("source_sense_ids") or [])[:20]},
        )
        return {"sense": item}

    @router.post("/builtin/social-research/slang/senses/{sense_id}/split")
    async def split_slang_sense(
        sense_id: str,
        response: Response,
        body: dict = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        try:
            item = learning_store().split_sense(
                sense_id,
                claim_ids=[str(value) for value in list(body.get("claim_ids") or [])],
                patch=body.get("sense") if isinstance(body.get("sense"), dict) else {},
                expected_revision=int(body.get("revision")),
                actor=admin.qq,
            )
        except Exception as exc:
            raise _safe_builtin_error(exc, "Sense 拆分失败") from exc
        webui_audit_log.record(
            action="meme_sense_split", qq=admin.qq, device_id=admin.device_id,
            target=sense_id, detail={"new_sense_id": item["sense_id"], "claim_count": len(body.get("claim_ids") or [])},
        )
        return {"sense": item}

    @router.get("/builtin/social-research/slang/events")
    async def slang_learning_events(
        response: Response,
        sense_id: str = "",
        limit: int = Query(200, ge=1, le=2000),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        items = learning_store().list_events(sense_id=sense_id, limit=limit)
        return {"events": items, "total": len(items)}

    return router
