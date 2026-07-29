from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from .routes.audit_routes import build_audit_router
from .routes.auth_routes import build_auth_router
from .routes.proactive_routes import build_proactive_router
from .routes.config_routes import build_config_router
from .routes.group_routes import build_group_router
from .routes.memory_routes import build_memory_router
from .routes.mcp_routes import build_mcp_router
from .routes.metrics_routes import build_metrics_router
from .routes.persona_routes import build_persona_router
from .routes.persona_template_routes import build_persona_template_router
from .routes.health_routes import build_health_router
from .routes.log_routes import build_log_router
from .routes.qq_routes import build_qq_router
from .routes.plugin_knowledge_routes import build_plugin_knowledge_router
from .routes.plugin_manager_routes import build_plugin_manager_router
from .routes.quota_routes import build_quota_router
from .routes.qzone_routes import build_qzone_router
from .routes.skill_routes import build_skill_router
from .routes.sticker_routes import build_sticker_router
from .routes.test_routes import build_test_router
from .routes.tool_creator_routes import build_tool_creator_router
from .routes.agent_status_routes import build_agent_status_router
from .routes.data_transfer_routes import build_data_transfer_router
from .routes.user_policy_routes import build_user_policy_router
from .routes.outbound_routes import build_outbound_router


@dataclass
class _RuntimeContext:
    plugin_config: Any
    superusers: set[str]
    get_bots: Callable[[], dict[str, Any]]
    logger: Any
    runtime_bundle: Any = None


_RUNTIME: _RuntimeContext | None = None
_WEBUI_INSTANCE_ID = secrets.token_urlsafe(18)


def set_runtime_context(
    *,
    plugin_config: Any,
    superusers: set[str],
    get_bots: Callable[[], dict[str, Any]],
    logger: Any,
    runtime_bundle: Any = None,
) -> None:
    global _RUNTIME
    _RUNTIME = _RuntimeContext(
        plugin_config=plugin_config,
        superusers=set(superusers or set()),
        get_bots=get_bots,
        logger=logger,
        runtime_bundle=runtime_bundle,
    )
    from ..core import admin_acl
    from . import deps

    def _is_current_admin(qq: str) -> bool:
        context = _RUNTIME
        if context is None:
            return False
        return qq in context.superusers or admin_acl.is_plugin_admin(qq)

    deps.set_admin_authorizer(_is_current_admin)


def get_runtime_context() -> _RuntimeContext:
    if _RUNTIME is None:
        raise RuntimeError("WebUI runtime context 未初始化")
    return _RUNTIME


def build_router() -> APIRouter:
    runtime = get_runtime_context()
    router = APIRouter(prefix="/personification")
    router.include_router(build_auth_router(runtime=runtime))
    router.include_router(build_config_router(runtime=runtime))
    router.include_router(build_metrics_router(runtime=runtime))
    router.include_router(build_persona_router(runtime=runtime))
    router.include_router(build_persona_template_router(runtime=runtime))
    router.include_router(build_group_router(runtime=runtime))
    router.include_router(build_skill_router(runtime=runtime))
    router.include_router(build_test_router(runtime=runtime))
    router.include_router(build_memory_router(runtime=runtime))
    router.include_router(build_mcp_router(runtime=runtime))
    router.include_router(build_sticker_router(runtime=runtime))
    router.include_router(build_audit_router(runtime=runtime))
    router.include_router(build_proactive_router(runtime=runtime))
    router.include_router(build_quota_router(runtime=runtime))
    router.include_router(build_qzone_router(runtime=runtime))
    router.include_router(build_plugin_knowledge_router(runtime=runtime))
    router.include_router(build_plugin_manager_router(runtime=runtime))
    router.include_router(build_health_router(runtime=runtime))
    router.include_router(build_log_router(runtime=runtime))
    router.include_router(build_qq_router(runtime=runtime))
    router.include_router(build_agent_status_router(runtime=runtime))
    router.include_router(build_data_transfer_router(runtime=runtime))
    router.include_router(build_tool_creator_router(runtime=runtime))
    router.include_router(build_user_policy_router(runtime=runtime))
    router.include_router(build_outbound_router(runtime=runtime))

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            _render_index_html(),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/static/{filename}")
    async def static_asset(filename: str, request: Request) -> FileResponse:
        return _serve_static_asset(filename, versioned=bool(request.query_params.get("v")))

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return router


_STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"
_STATIC_ROOT = _STATIC_INDEX_PATH.parent
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _load_index_html() -> str:
    return _STATIC_INDEX_PATH.read_text(encoding="utf-8")


def _asset_version(filename: str) -> str:
    target = (_STATIC_ROOT / filename).resolve()
    try:
        stat = target.stat()
    except OSError:
        return str(int(time.time()))
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def _render_index_html() -> str:
    html = _load_index_html()
    assets = (
        "style.css",
        "app-core.js",
        "app-activity.js",
        "app-content.js",
        "app-admin.js",
        "app-tools.js",
        "app-mcp.js",
        "app-tool-creator.js",
        "app-config.js",
        "app-auth.js",
        "app-operations.js",
    )
    versions = {filename: _asset_version(filename) for filename in assets}
    html = html.replace("__PERSONIFICATION_WEBUI_INSTANCE_ID__", json.dumps(_WEBUI_INSTANCE_ID))
    html = html.replace("__PERSONIFICATION_ASSET_VERSIONS__", json.dumps(versions, ensure_ascii=False))
    for filename in assets:
        html = html.replace(
            f"/personification/static/{filename}",
            f"/personification/static/{filename}?v={_asset_version(filename)}",
        )
    return html


def _serve_static_asset(filename: str, *, versioned: bool = False) -> FileResponse:
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="static asset not found")
    target = (_STATIC_ROOT / filename).resolve()
    try:
        target.relative_to(_STATIC_ROOT.resolve())
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="static asset not found") from exc
    media_type = _STATIC_CONTENT_TYPES.get(target.suffix.lower())
    if media_type is None or not target.is_file():
        raise HTTPException(status_code=404, detail="static asset not found")
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable"
        if versioned
        else "no-cache, max-age=0, must-revalidate"
    }
    return FileResponse(target, media_type=media_type, headers=headers)
