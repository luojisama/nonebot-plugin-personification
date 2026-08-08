from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from ...core import webui_audit_log
from ...core.gemini_web_service import get_gemini_web_service
from ...core.mimo_web_asr_service import get_mimo_web_asr_service
from ..deps import AdminIdentity, get_client_ip, require_admin


@dataclass(frozen=True)
class _ServiceSpec:
    name: str
    title: str
    code_prefix: str
    contract: str
    platform: str
    logout_confirm: str
    factory: Callable[[Any], Any]


_SERVICE_SPECS = {
    "gemini": _ServiceSpec(
        name="gemini",
        title="Gemini Web",
        code_prefix="gemini_web",
        contract="gemini_web_v1",
        platform="gemini_web",
        logout_confirm="确认注销GeminiWeb",
        factory=get_gemini_web_service,
    ),
    "mimo_asr": _ServiceSpec(
        name="mimo_asr",
        title="MiMo Web ASR",
        code_prefix="mimo_web_asr",
        contract="mimo_studio_asr_v1",
        platform="mimo_asr_web",
        logout_confirm="确认注销MiMoWebASR",
        factory=get_mimo_web_asr_service,
    ),
}


def _private(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _resolve(runtime: Any, service_name: str) -> tuple[Any, _ServiceSpec]:
    spec = _SERVICE_SPECS.get(str(service_name or "").strip())
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "code": "consumer_web_service_not_found", "message": "未知消费者网页服务。"},
        )
    return spec.factory(runtime), spec


def _owner(admin: AdminIdentity, spec: _ServiceSpec) -> str:
    return f"{admin.qq}:{admin.device_id}:{spec.platform}"


def _safe_error(exc: Exception, title: str, spec: _ServiceSpec) -> HTTPException:
    raw = str(exc or "").strip()
    known_suffixes = {
        "disabled",
        "risk_ack_required",
        "login_required",
        "manual_verification_required",
        "network_risk_detected",
        "network_risk_cooldown",
        "busy",
        "dom_changed",
        "upload_rejected",
        "generation_timeout",
        "output_empty",
        "process_failed",
        "context_idle_evicted",
        "media_too_large",
        "request_invalid",
        "local_rate_limited",
        "model_unavailable",
    }
    code = raw if any(raw == f"{spec.code_prefix}_{suffix}" for suffix in known_suffixes) else f"{spec.code_prefix}_operation_failed"
    status = (
        409
        if code.endswith(("_busy", "_network_risk_cooldown", "_local_rate_limited"))
        else 400
        if code.endswith(("_risk_ack_required", "_request_invalid"))
        else 503
    )
    messages = {
        "risk_ack_required": f"启用或操作 {spec.title} 前必须先确认第三方上传与消费者网页自动化风险。",
        "disabled": f"{spec.title} 当前未启用，请先保存启用与风险确认配置。",
        "busy": "另一个消费者网页媒体任务、登录或人工接管正在运行。",
        "network_risk_detected": "页面提示网络或账号安全风险，自动操作已立即停止并进入冷却。",
        "network_risk_cooldown": "网络风险冷却尚未结束；请等待冷却或改用正式 API。",
        "local_rate_limited": f"{spec.title} 本地调用频率已达到安全上限，本次将改用其他媒体路径。",
        "login_required": f"{spec.title} 登录态不可用，请由管理员打开人工登录。",
        "manual_verification_required": "官方页面要求人工验证，自动操作已停止。",
        "dom_changed": f"{spec.title} 页面结构与当前适配器不匹配，已停止自动操作。",
        "model_unavailable": "MiMo Studio 当前页面没有可选择的 MiMo-V2.5-ASR，已停止自动操作。",
    }
    suffix = code.removeprefix(f"{spec.code_prefix}_")
    return HTTPException(
        status_code=status,
        detail={
            "ok": False,
            "code": code,
            "title": title,
            "message": messages.get(suffix, f"{spec.title} 操作未完成；请查看脱敏诊断后改用其他媒体路径。"),
            "error_type": type(exc).__name__,
        },
    )


def build_consumer_web_router(*, runtime: Any) -> APIRouter:
    router = APIRouter(prefix="/api/media/web/{service_name}", tags=["consumer-media-web"])

    @router.get("/status")
    async def status(
        service_name: str,
        response: Response,
        refresh: bool = Query(default=False),
        _: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, _spec = _resolve(runtime, service_name)
        return await service.status(runtime.plugin_config, refresh=refresh)

    @router.post("/probe")
    async def probe(
        service_name: str,
        request: Request,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        try:
            result = await service.probe(runtime.plugin_config)
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 页面兼容性检查未完成", spec) from exc
        webui_audit_log.record(
            action=f"{spec.code_prefix}_probe",
            qq=admin.qq,
            device_id=admin.device_id,
            target=spec.contract,
            ip_hash=get_client_ip(request),
            detail={"state": str(result.get("state") or "unknown")},
            outcome="success" if result.get("state") == "ready" else "partial",
        )
        return result

    @router.post("/auth/start")
    async def auth_start(
        service_name: str,
        request: Request,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        try:
            result = await service.auth_start(runtime.plugin_config, _owner(admin, spec))
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 人工登录会话创建失败", spec) from exc
        webui_audit_log.record(
            action=f"{spec.code_prefix}_auth_start",
            qq=admin.qq,
            device_id=admin.device_id,
            target=spec.platform,
            ip_hash=get_client_ip(request),
            detail={"page_contract_version": spec.contract},
        )
        return result

    @router.get("/auth/{session_id}")
    async def auth_status(
        service_name: str,
        session_id: str,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        try:
            return await service.auth_status(runtime.plugin_config, session_id, _owner(admin, spec))
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 人工登录状态读取失败", spec) from exc

    @router.get("/auth/{session_id}/frame")
    async def auth_frame(
        service_name: str,
        session_id: str,
        revision: int = Query(default=0, ge=0, le=2_147_483_647),
        admin: AdminIdentity = Depends(require_admin),
    ) -> Response:
        service, spec = _resolve(runtime, service_name)
        try:
            result = await service.auth_frame(
                runtime.plugin_config,
                session_id,
                _owner(admin, spec),
                after_revision=revision,
            )
            frame_revision = max(0, int(result.get("interactive_frame_revision") or 0))
            headers = {
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
                "X-Interactive-Revision": str(frame_revision),
                "X-Interactive-Stale": "1" if result.get("stale") else "0",
            }
            if not bool(result.get("changed", bool(result.get("data_base64")))):
                return Response(status_code=204, headers=headers)
            image = base64.b64decode(str(result.get("data_base64") or ""), validate=True)
            mime_type = str(result.get("mime_type") or "")
            if mime_type not in {"image/jpeg", "image/png"} or not image or len(image) > 2 * 1024 * 1024:
                raise ValueError(f"{spec.code_prefix}_process_failed")
            return Response(content=image, media_type=mime_type, headers=headers)
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 人工接管画面读取失败", spec) from exc

    @router.post("/auth/{session_id}/input")
    async def auth_input(
        service_name: str,
        session_id: str,
        response: Response,
        body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        action = body.get("action")
        if not isinstance(action, dict) or len(json.dumps(action, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 16 * 1024:
            raise _safe_error(ValueError(f"{spec.code_prefix}_request_invalid"), f"{spec.title} 人工接管操作无效", spec)
        try:
            return await service.auth_input(runtime.plugin_config, session_id, _owner(admin, spec), action)
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 人工接管操作未完成", spec) from exc

    @router.post("/auth/{session_id}/finish")
    async def auth_finish(
        service_name: str,
        session_id: str,
        request: Request,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        try:
            result = await service.auth_finish(runtime.plugin_config, session_id, _owner(admin, spec))
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 登录状态确认失败", spec) from exc
        webui_audit_log.record(
            action=f"{spec.code_prefix}_auth_finish",
            qq=admin.qq,
            device_id=admin.device_id,
            target=spec.platform,
            ip_hash=get_client_ip(request),
            detail={"status": str(result.get("status") or "unknown")},
            outcome="success" if result.get("status") == "success" else "partial",
        )
        return result

    @router.post("/auth/{session_id}/cancel")
    async def auth_cancel(
        service_name: str,
        session_id: str,
        response: Response,
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        try:
            return await service.auth_cancel(runtime.plugin_config, session_id, _owner(admin, spec))
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 人工登录会话取消失败", spec) from exc

    @router.post("/logout")
    async def logout(
        service_name: str,
        request: Request,
        response: Response,
        body: dict[str, Any] = Body(default_factory=dict),
        admin: AdminIdentity = Depends(require_admin),
    ) -> dict[str, Any]:
        _private(response)
        service, spec = _resolve(runtime, service_name)
        if str(body.get("confirm") or "") != spec.logout_confirm:
            raise _safe_error(
                ValueError(f"{spec.code_prefix}_request_invalid"),
                f"请输入精确确认文本：{spec.logout_confirm}",
                spec,
            )
        try:
            result = await service.logout(runtime.plugin_config)
        except Exception as exc:
            raise _safe_error(exc, f"{spec.title} 本地登录 profile 注销失败", spec) from exc
        webui_audit_log.record(
            action=f"{spec.code_prefix}_logout",
            qq=admin.qq,
            device_id=admin.device_id,
            target=spec.platform,
            ip_hash=get_client_ip(request),
            detail={"profile_deleted": True},
        )
        return result

    return router


__all__ = ["build_consumer_web_router"]
