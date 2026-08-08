from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from .routes.audit_routes import build_audit_router
from .routes.auth_routes import build_auth_router
from .routes.proactive_routes import build_proactive_router
from .routes.config_routes import build_config_router
from .routes.group_routes import build_group_router
from .routes.memory_routes import build_memory_router
from .routes.mcp_routes import build_mcp_router
from .routes.consumer_web_routes import build_consumer_web_router
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
from .routes.performance_routes import build_performance_router
from ..core.runtime_performance import register_cache_reporter


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
    router.include_router(build_consumer_web_router(runtime=runtime))
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
    router.include_router(build_performance_router(runtime=runtime))

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(
            _render_index_html(),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @router.get("/static/{filename}")
    async def static_asset(filename: str, request: Request) -> Response:
        return await asyncio.to_thread(
            _serve_static_asset,
            filename,
            versioned=bool(request.query_params.get("v")),
            accept_encoding=str(request.headers.get("accept-encoding", "") or ""),
            if_none_match=str(request.headers.get("if-none-match", "") or ""),
        )

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
_STATIC_GZIP_CACHE_MAX_SIZE = 16
_STATIC_GZIP_CACHE: OrderedDict[tuple[str, int, int], bytes] = OrderedDict()
_STATIC_GZIP_CACHE_LOCK = threading.RLock()
_STATIC_GZIP_CACHE_EVICTIONS = 0


def _static_gzip_cache_snapshot() -> dict[str, int]:
    with _STATIC_GZIP_CACHE_LOCK:
        return {
            "entries": len(_STATIC_GZIP_CACHE),
            "limit": _STATIC_GZIP_CACHE_MAX_SIZE,
            "evictions": _STATIC_GZIP_CACHE_EVICTIONS,
        }


register_cache_reporter("webui_static_gzip", _static_gzip_cache_snapshot)


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
        "app-admin-common.js",
        "app-dashboard.js",
        "app-health-qq.js",
        "app-qzone.js",
        "app-identity-policy.js",
        "app-persona-builder.js",
        "app-groups.js",
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


def _accepts_gzip(value: str) -> bool:
    for item in str(value or "").lower().split(","):
        encoding, _, params = item.strip().partition(";")
        if encoding != "gzip":
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, raw_value = param.strip().partition("=")
            if key == "q":
                try:
                    quality = float(raw_value)
                except ValueError:
                    quality = 0.0
        return quality > 0
    return False


def _gzip_asset(target: Path, *, filename: str, mtime_ns: int, size: int) -> bytes:
    global _STATIC_GZIP_CACHE_EVICTIONS
    key = (filename, int(mtime_ns), int(size))
    with _STATIC_GZIP_CACHE_LOCK:
        cached = _STATIC_GZIP_CACHE.get(key)
        if cached is not None:
            _STATIC_GZIP_CACHE.move_to_end(key)
            return cached
    compressed = gzip.compress(target.read_bytes(), compresslevel=6, mtime=0)
    with _STATIC_GZIP_CACHE_LOCK:
        _STATIC_GZIP_CACHE[key] = compressed
        _STATIC_GZIP_CACHE.move_to_end(key)
        stale = [item for item in _STATIC_GZIP_CACHE if item[0] == filename and item != key]
        for item in stale:
            _STATIC_GZIP_CACHE.pop(item, None)
            _STATIC_GZIP_CACHE_EVICTIONS += 1
        while len(_STATIC_GZIP_CACHE) > _STATIC_GZIP_CACHE_MAX_SIZE:
            _STATIC_GZIP_CACHE.popitem(last=False)
            _STATIC_GZIP_CACHE_EVICTIONS += 1
    return compressed


def _serve_static_asset(
    filename: str,
    *,
    versioned: bool = False,
    accept_encoding: str = "",
    if_none_match: str = "",
) -> Response:
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
    stat = target.stat()
    identity = f"{filename}:{int(stat.st_mtime_ns)}:{int(stat.st_size)}".encode("utf-8")
    etag = f'W/"{hashlib.sha256(identity).hexdigest()[:20]}"'
    headers = {
        "Cache-Control": "public, max-age=31536000, immutable"
        if versioned
        else "no-cache, max-age=0, must-revalidate",
        "ETag": etag,
        "Vary": "Accept-Encoding",
    }
    if str(if_none_match or "").strip() == etag:
        return Response(status_code=304, headers=headers)
    if _accepts_gzip(accept_encoding):
        headers["Content-Encoding"] = "gzip"
        return Response(
            content=_gzip_asset(
                target,
                filename=filename,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
            ),
            media_type=media_type,
            headers=headers,
        )
    return FileResponse(target, media_type=media_type, headers=headers)
