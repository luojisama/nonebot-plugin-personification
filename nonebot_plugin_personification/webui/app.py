from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from .routes.audit_routes import build_audit_router
from .routes.auth_routes import build_auth_router
from .routes.proactive_routes import build_proactive_router
from .routes.config_routes import build_config_router
from .routes.group_routes import build_group_router
from .routes.memory_routes import build_memory_router
from .routes.metrics_routes import build_metrics_router
from .routes.persona_routes import build_persona_router
from .routes.health_routes import build_health_router
from .routes.log_routes import build_log_router
from .routes.qq_routes import build_qq_router
from .routes.plugin_knowledge_routes import build_plugin_knowledge_router
from .routes.quota_routes import build_quota_router
from .routes.qzone_routes import build_qzone_router
from .routes.skill_routes import build_skill_router
from .routes.sticker_routes import build_sticker_router
from .routes.test_routes import build_test_router


@dataclass
class _RuntimeContext:
    plugin_config: Any
    superusers: set[str]
    get_bots: Callable[[], dict[str, Any]]
    logger: Any
    runtime_bundle: Any = None


_RUNTIME: _RuntimeContext | None = None


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
    router.include_router(build_group_router(runtime=runtime))
    router.include_router(build_skill_router(runtime=runtime))
    router.include_router(build_test_router(runtime=runtime))
    router.include_router(build_memory_router(runtime=runtime))
    router.include_router(build_sticker_router(runtime=runtime))
    router.include_router(build_audit_router(runtime=runtime))
    router.include_router(build_proactive_router(runtime=runtime))
    router.include_router(build_quota_router(runtime=runtime))
    router.include_router(build_qzone_router(runtime=runtime))
    router.include_router(build_plugin_knowledge_router(runtime=runtime))
    router.include_router(build_health_router(runtime=runtime))
    router.include_router(build_log_router(runtime=runtime))
    router.include_router(build_qq_router(runtime=runtime))

    @router.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_INDEX_HTML)

    @router.get("/static/{filename}")
    async def static_asset(filename: str) -> FileResponse:
        return _serve_static_asset(filename)

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return router


_STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"
_STATIC_ROOT = _STATIC_INDEX_PATH.parent
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def _load_index_html() -> str:
    return _STATIC_INDEX_PATH.read_text(encoding="utf-8")


def _serve_static_asset(filename: str) -> FileResponse:
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
    return FileResponse(target, media_type=media_type)


_INDEX_HTML = _load_index_html()
