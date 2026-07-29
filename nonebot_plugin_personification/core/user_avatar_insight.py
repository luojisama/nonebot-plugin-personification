from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import time
from typing import Any, Awaitable, Callable

from .evidence_envelope import EvidenceEnvelope
from .media_understanding import analyze_images_with_route_or_fallback
from .safe_image_download import download_public_image
from .visual_capabilities import VISUAL_ROUTE_VISION


AVATAR_ANALYSIS_SCHEMA_VERSION = 1
AVATAR_MAX_BYTES = 5 * 1024 * 1024
AVATAR_SUCCESS_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
AVATAR_FAILURE_COOLDOWN_SECONDS = 60 * 60
AVATAR_VISION_TIMEOUT_SECONDS = 45.0
AVATAR_ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp"}
AVATAR_ASSET_KINDS = {
    "real_person",
    "illustration",
    "acg_character",
    "logo",
    "other",
    "unknown",
}
_INSIGHT_FIELDS = {
    "asset_kind",
    "subject_count",
    "neutral_summary",
    "acg_candidates",
    "contains_text",
    "confidence",
}
_AVATAR_PROMPT = """\
分析这张 QQ 头像，只输出一个严格 JSON object，不要 Markdown、解释或额外字段：
{"asset_kind":"real_person|illustration|acg_character|logo|other|unknown","subject_count":0,"neutral_summary":"中性短摘要","acg_candidates":["可能的角色或作品"],"contains_text":false,"confidence":0.0}

安全边界：
- 如果是真人或疑似真人照片，asset_kind 必须为 real_person，neutral_summary 只能写“真人头像”，acg_candidates 必须为空。
- 禁止从真人头像推断或描述身份、姓名、性别、年龄、外貌细节、性格、精神或情绪状态、职业、现实人际关系。
- 不做人脸识别，不把画中主体当作 QQ 用户本人。
- ACG 候选只能作为不确定候选；无法可靠判断时使用 unknown 和空列表。
"""

_IN_FLIGHT: dict[tuple[int, str], asyncio.Task[dict[str, Any]]] = {}
_IN_FLIGHT_FORCE: dict[tuple[int, str], bool] = {}
_IN_FLIGHT_PREDECESSORS: dict[tuple[int, str], asyncio.Task[dict[str, Any]]] = {}
_IN_FLIGHT_PROFILE_SERVICES: dict[tuple[int, str], Any] = {}
_PROFILE_GENERATIONS: dict[tuple[int, str], int] = {}
_URL_TEXT_RE = re.compile(r"(?:https?://|www\.|data:)\S+", re.IGNORECASE)


def _reply_runtime(runtime: Any) -> Any:
    holder = getattr(runtime, "runtime_bundle", None) or runtime
    deps = getattr(holder, "reply_processor_deps", None)
    inner = getattr(deps, "runtime", None) if deps is not None else None
    return inner or holder


def _logger(runtime: Any) -> Any:
    return getattr(_reply_runtime(runtime), "logger", None)


def _profile_service(runtime: Any) -> Any:
    return getattr(_reply_runtime(runtime), "profile_service", None)


def _persona_enabled(runtime: Any) -> bool:
    config = getattr(_reply_runtime(runtime), "plugin_config", None)
    return bool(getattr(config, "personification_persona_enabled", True))


def _profile_generation_key(profile_service: Any, user_id: str) -> tuple[int, str]:
    return id(profile_service), str(user_id or "").strip()


def _profile_generation(profile_service: Any, user_id: str) -> int:
    return _PROFILE_GENERATIONS.get(_profile_generation_key(profile_service, user_id), 0)


def _bounded_text(value: Any, limit: int) -> str:
    text = _URL_TEXT_RE.sub("", str(value or ""))
    return " ".join(text.strip().split())[: max(1, int(limit))]


def normalize_avatar_insight(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _INSIGHT_FIELDS:
        raise ValueError("avatar insight does not match the required schema")
    asset_kind = str(value.get("asset_kind", "") or "").strip().lower()
    if asset_kind not in AVATAR_ASSET_KINDS:
        raise ValueError("avatar asset_kind is invalid")
    subject_count = value.get("subject_count")
    if isinstance(subject_count, bool) or not isinstance(subject_count, int):
        raise ValueError("avatar subject_count must be an integer")
    subject_count = max(0, min(subject_count, 20))
    contains_text = value.get("contains_text")
    if not isinstance(contains_text, bool):
        raise ValueError("avatar contains_text must be a boolean")
    confidence = value.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("avatar confidence must be numeric")
    confidence = max(0.0, min(float(confidence), 1.0))
    raw_candidates = value.get("acg_candidates")
    if not isinstance(raw_candidates, list):
        raise ValueError("avatar acg_candidates must be an array")
    candidates: list[str] = []
    for item in raw_candidates:
        if not isinstance(item, str):
            raise ValueError("avatar acg_candidates must contain strings")
        candidate = _bounded_text(item, 80)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
        if len(candidates) >= 8:
            break
    summary = _bounded_text(value.get("neutral_summary"), 240)
    if asset_kind == "real_person":
        summary = "真人头像"
        candidates = []
    elif asset_kind == "unknown":
        summary = "头像内容无法可靠归类"
        candidates = []
    elif not summary:
        raise ValueError("avatar neutral_summary is required")
    return {
        "asset_kind": asset_kind,
        "subject_count": subject_count,
        "neutral_summary": summary,
        "acg_candidates": candidates,
        "contains_text": contains_text,
        "confidence": confidence,
    }


def _parse_avatar_response(raw: str) -> dict[str, Any]:
    value = json.loads(str(raw or "").strip())
    return normalize_avatar_insight(value)


def _avatar_state(profile_json: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    root = profile_json if isinstance(profile_json, dict) else {}
    qq_profile = root.get("qq_profile") if isinstance(root.get("qq_profile"), dict) else {}
    analysis = qq_profile.get("avatar_analysis") if isinstance(qq_profile.get("avatar_analysis"), dict) else {}
    insight = qq_profile.get("avatar_insight") if isinstance(qq_profile.get("avatar_insight"), dict) else {}
    return dict(analysis), dict(insight)


def _patch_avatar_state(
    profile_json: dict[str, Any],
    *,
    analysis: dict[str, Any] | None,
    insight: dict[str, Any] | None,
    remove: bool = False,
) -> dict[str, Any]:
    patched = dict(profile_json or {})
    qq_profile = dict(patched.get("qq_profile", {}) or {})
    if remove:
        qq_profile.pop("avatar_analysis", None)
        qq_profile.pop("avatar_insight", None)
    else:
        if analysis is not None:
            qq_profile["avatar_analysis"] = dict(analysis)
        if insight is None:
            qq_profile.pop("avatar_insight", None)
        else:
            qq_profile["avatar_insight"] = dict(insight)
    patched["qq_profile"] = qq_profile
    return patched


def _analysis_payload(
    *,
    checked_at: float,
    content_hash: str,
    analyzed_at: float,
    status: str,
    route: str,
) -> dict[str, Any]:
    return {
        "checked_at": float(checked_at),
        "content_hash": str(content_hash or ""),
        "analyzed_at": float(analyzed_at),
        "status": str(status or "unknown"),
        "schema_version": AVATAR_ANALYSIS_SCHEMA_VERSION,
        "route": _bounded_text(route, 80),
    }


def serialize_avatar_state(
    profile_json: dict[str, Any] | None,
    *,
    include_hash: bool = False,
    include_hash_short: bool = True,
) -> dict[str, Any]:
    analysis, insight = _avatar_state(profile_json)
    safe_analysis = {
        "checked_at": float(analysis.get("checked_at", 0) or 0),
        "analyzed_at": float(analysis.get("analyzed_at", 0) or 0),
        "status": _bounded_text(analysis.get("status"), 32),
        "schema_version": int(analysis.get("schema_version", 0) or 0),
        "route": _bounded_text(analysis.get("route"), 80),
    }
    content_hash = str(analysis.get("content_hash", "") or "")
    if include_hash:
        safe_analysis["content_hash"] = content_hash
    if include_hash_short:
        safe_analysis["content_hash_short"] = content_hash[:12]
    try:
        safe_insight = normalize_avatar_insight(insight) if insight else {}
    except (TypeError, ValueError):
        safe_insight = {}
    return {"analysis": safe_analysis, "insight": safe_insight}


def render_avatar_persona_context(profile_json: dict[str, Any] | None) -> str:
    state = serialize_avatar_state(profile_json)
    insight = state.get("insight", {})
    if not insight:
        return ""
    parts = [
        f"类型：{insight['asset_kind']}",
        f"中性摘要：{insight['neutral_summary']}",
    ]
    candidates = list(insight.get("acg_candidates") or [])
    if candidates:
        parts.append("ACG 候选：" + "、".join(candidates[:5]))
    parts.append(f"置信度：{float(insight.get('confidence', 0)):.2f}")
    return "；".join(parts)


def get_user_avatar_state(
    profile_service: Any,
    user_id: str,
    *,
    include_hash: bool = False,
    include_hash_short: bool = True,
) -> dict[str, Any]:
    snapshot = profile_service.get_core_profile(str(user_id or "")) if profile_service is not None else None
    return serialize_avatar_state(
        snapshot.profile_json if snapshot is not None else {},
        include_hash=include_hash,
        include_hash_short=include_hash_short,
    )


def build_avatar_evidence_envelope(
    insight: dict[str, Any] | None = None,
) -> EvidenceEnvelope:
    current = dict(insight or {})
    summary = " ".join(str(current.get("neutral_summary", "") or "").split())[:240]
    try:
        confidence = float(current.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    if not math.isfinite(confidence):
        confidence = 0.0
    claims: list[str] = []
    if summary:
        claims.append(f"头像画面可描述为：{summary}")
    candidates = [
        " ".join(str(item or "").split())[:80]
        for item in list(current.get("acg_candidates") or [])[:3]
        if " ".join(str(item or "").split())
    ]
    if candidates:
        claims.append("可能的虚构角色或作品候选：" + "、".join(candidates))
    fallback = (
        f"看得到，头像里像是{summary.rstrip('。')}。"
        if summary
        else "这会儿没看清头像里是什么。"
    )
    return EvidenceEnvelope(
        allowed_claims=tuple(claims),
        forbidden_inferences=(
            "不得把头像中的人物、角色、性别、年龄或属性说成用户本人。",
            "不得推断真人身份、现实关系、性格、经历或其他未观察到的事实。",
            "不得提及系统、工具、接口、摘要、安全分析、内部机制或内容策略。",
        ),
        confidence=max(0.0, min(1.0, confidence)),
        natural_fallback=fallback,
        available=bool(summary),
    )


def _cooldown_reason(analysis: dict[str, Any], now_ts: float, force: bool) -> str:
    if force:
        return ""
    if int(analysis.get("schema_version", 0) or 0) != AVATAR_ANALYSIS_SCHEMA_VERSION:
        return ""
    checked_at = float(analysis.get("checked_at", 0) or 0)
    elapsed = max(0.0, float(now_ts) - checked_at)
    status = str(analysis.get("status", "") or "")
    if status in {"success", "unchanged"} and checked_at and elapsed < AVATAR_SUCCESS_CHECK_INTERVAL_SECONDS:
        return "success_check_interval"
    if status == "failed" and checked_at and elapsed < AVATAR_FAILURE_COOLDOWN_SECONDS:
        return "failure_cooldown"
    return ""


async def analyze_user_avatar(
    runtime: Any,
    user_id: str,
    *,
    force: bool = False,
    now_ts: float | None = None,
    downloader: Callable[..., Awaitable[Any]] | None = None,
    analyzer: Callable[..., Awaitable[tuple[str, str]]] | None = None,
) -> dict[str, Any]:
    runtime = _reply_runtime(runtime)
    profile_service = _profile_service(runtime)
    uid = str(user_id or "").strip()
    if profile_service is None or not uid or not _persona_enabled(runtime):
        return {"status": "unavailable", "reason": "profile_service_unavailable"}
    memory_store = getattr(profile_service, "memory_store", None)
    generation_getter = getattr(memory_store, "get_profile_generation", None)
    profile_generation = int(generation_getter()) if callable(generation_getter) else None
    snapshot = profile_service.get_core_profile(uid)
    if snapshot is None:
        return {"status": "unavailable", "reason": "profile_missing"}
    profile_json = dict(snapshot.profile_json or {})
    qq_profile = profile_json.get("qq_profile") if isinstance(profile_json.get("qq_profile"), dict) else {}
    avatar_url = str(qq_profile.get("avatar_url", "") or "").strip()
    if not avatar_url:
        return {"status": "unavailable", "reason": "avatar_url_missing"}
    previous_analysis, previous_insight = _avatar_state(profile_json)
    generation = _profile_generation(profile_service, uid)

    def _patch_if_current(patcher: Callable[[dict[str, Any]], dict[str, Any]]) -> bool:
        if (
            not _persona_enabled(runtime)
            or _profile_generation(profile_service, uid) != generation
        ):
            return False
        try:
            profile_service.patch_core_profile(
                user_id=uid,
                patcher=patcher,
                expected_generation=profile_generation,
            )
        except Exception as exc:
            if getattr(exc, "code", "") == "profile_generation_changed":
                return False
            raise
        return True

    checked_at = float(now_ts if now_ts is not None else time.time())
    cooldown = _cooldown_reason(previous_analysis, checked_at, force)
    if cooldown:
        return {
            "status": str(previous_analysis.get("status", "") or "skipped"),
            "reason": cooldown,
            "skipped": True,
        }

    download = downloader or download_public_image
    analyze = analyzer or analyze_images_with_route_or_fallback
    try:
        downloaded = await download(
            avatar_url,
            max_bytes=AVATAR_MAX_BYTES,
            allowed_mimes=set(AVATAR_ALLOWED_MIMES),
        )
    except Exception:
        failed = _analysis_payload(
            checked_at=checked_at,
            content_hash=str(previous_analysis.get("content_hash", "") or ""),
            analyzed_at=float(previous_analysis.get("analyzed_at", 0) or 0),
            status="failed",
            route="download_failed",
        )
        persisted = _patch_if_current(
            lambda current: _patch_avatar_state(
                current,
                analysis=failed,
                insight=previous_insight or None,
            )
        )
        if not persisted:
            return {"status": "cancelled", "reason": "profile_generation_changed", "skipped": True}
        return {"status": "failed", "reason": "download_failed", "skipped": False}

    content = bytes(downloaded.content)
    content_hash = hashlib.sha256(content).hexdigest()
    previous_hash = str(previous_analysis.get("content_hash", "") or "")
    schema_current = int(previous_analysis.get("schema_version", 0) or 0) == AVATAR_ANALYSIS_SCHEMA_VERSION
    if not force and schema_current and previous_hash == content_hash and previous_insight:
        unchanged = _analysis_payload(
            checked_at=checked_at,
            content_hash=content_hash,
            analyzed_at=float(previous_analysis.get("analyzed_at", 0) or 0),
            status="unchanged",
            route=str(previous_analysis.get("route", "") or "hash_unchanged"),
        )
        persisted = _patch_if_current(
            lambda current: _patch_avatar_state(
                current,
                analysis=unchanged,
                insight=previous_insight,
            )
        )
        if not persisted:
            return {"status": "cancelled", "reason": "profile_generation_changed", "skipped": True}
        return {"status": "unchanged", "reason": "content_hash_unchanged", "skipped": True}

    if not _persona_enabled(runtime):
        return {"status": "cancelled", "reason": "persona_disabled", "skipped": True}

    data_url = f"data:{downloaded.content_type};base64,{base64.b64encode(content).decode('ascii')}"
    route = "vision_unavailable"
    try:
        raw_response, route = await asyncio.wait_for(
            analyze(
                runtime=runtime,
                prompt=_AVATAR_PROMPT,
                image_refs=[data_url],
                route_name=VISUAL_ROUTE_VISION,
                image_detail="low",
            ),
            timeout=AVATAR_VISION_TIMEOUT_SECONDS,
        )
        insight = _parse_avatar_response(raw_response)
    except Exception:
        same_content = previous_hash == content_hash
        failed = _analysis_payload(
            checked_at=checked_at,
            content_hash=content_hash,
            analyzed_at=float(previous_analysis.get("analyzed_at", 0) or 0) if same_content else 0.0,
            status="failed",
            route=route or "vision_failed",
        )
        persisted = _patch_if_current(
            lambda current: _patch_avatar_state(
                current,
                analysis=failed,
                insight=previous_insight if same_content and previous_insight else None,
            )
        )
        if not persisted:
            return {"status": "cancelled", "reason": "profile_generation_changed", "skipped": True}
        return {"status": "failed", "reason": "vision_failed", "skipped": False}
    finally:
        del data_url
        del content

    succeeded = _analysis_payload(
        checked_at=checked_at,
        content_hash=content_hash,
        analyzed_at=checked_at,
        status="success",
        route=route,
    )
    persisted = _patch_if_current(
        lambda current: _patch_avatar_state(
            current,
            analysis=succeeded,
            insight=insight,
        )
    )
    if not persisted:
        return {"status": "cancelled", "reason": "profile_generation_changed", "skipped": True}
    return {
        "status": "success",
        "reason": "analyzed",
        "skipped": False,
        "content_hash": content_hash,
        "route": route,
        "insight": insight,
    }


def schedule_user_avatar_analysis(runtime: Any, user_id: str, *, force: bool = False) -> asyncio.Task[dict[str, Any]] | None:
    runtime = _reply_runtime(runtime)
    uid = str(user_id or "").strip()
    profile_service = _profile_service(runtime)
    if not uid or profile_service is None:
        return None
    key = (id(runtime), uid)
    current = _IN_FLIGHT.get(key)
    if current is not None and not current.done():
        if not force or _IN_FLIGHT_FORCE.get(key, False):
            return current

        async def _force_after_current() -> dict[str, Any]:
            try:
                await current
            except Exception:
                pass
            return await analyze_user_avatar(runtime, uid, force=True)

        task = asyncio.create_task(_force_after_current())
        _IN_FLIGHT_PREDECESSORS[key] = current
    else:
        if not force:
            snapshot = profile_service.get_core_profile(uid)
            profile_json = snapshot.profile_json if snapshot is not None else {}
            analysis, _insight = _avatar_state(profile_json)
            if _cooldown_reason(analysis, time.time(), False):
                return None
        task = asyncio.create_task(analyze_user_avatar(runtime, uid, force=force))
    _IN_FLIGHT[key] = task
    _IN_FLIGHT_FORCE[key] = bool(force)
    _IN_FLIGHT_PROFILE_SERVICES[key] = profile_service

    def _done(completed: asyncio.Task[dict[str, Any]]) -> None:
        if _IN_FLIGHT.get(key) is completed:
            _IN_FLIGHT.pop(key, None)
            _IN_FLIGHT_FORCE.pop(key, None)
            _IN_FLIGHT_PREDECESSORS.pop(key, None)
            _IN_FLIGHT_PROFILE_SERVICES.pop(key, None)
        try:
            completed.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger = _logger(runtime)
            if logger is not None:
                logger.warning(f"[avatar_insight] background analysis failed type={type(exc).__name__}")

    task.add_done_callback(_done)
    return task


def force_user_avatar_analysis(runtime: Any, user_id: str) -> asyncio.Task[dict[str, Any]] | None:
    return schedule_user_avatar_analysis(runtime, user_id, force=True)


def clear_user_avatar_analysis(profile_service: Any, user_id: str) -> bool:
    uid = str(user_id or "").strip()
    try:
        from .user_avatar_pair_insight import clear_user_avatar_pair_analysis

        clear_user_avatar_pair_analysis(uid)
    except Exception:
        pass
    generation_key = _profile_generation_key(profile_service, uid)
    _PROFILE_GENERATIONS[generation_key] = _PROFILE_GENERATIONS.get(generation_key, 0) + 1
    for key, task in list(_IN_FLIGHT.items()):
        if key[1] != uid or _IN_FLIGHT_PROFILE_SERVICES.get(key) is not profile_service:
            continue
        predecessor = _IN_FLIGHT_PREDECESSORS.pop(key, None)
        if predecessor is not None and not predecessor.done():
            predecessor.cancel()
        if not task.done():
            task.cancel()
    memory_store = getattr(profile_service, "memory_store", None)
    generation_getter = getattr(memory_store, "get_profile_generation", None)
    profile_generation = int(generation_getter()) if callable(generation_getter) else None
    snapshot = profile_service.get_core_profile(uid) if profile_service is not None and uid else None
    if snapshot is None:
        return False
    analysis, insight = _avatar_state(snapshot.profile_json)
    existed = bool(analysis or insight)
    if not existed:
        return False
    try:
        profile_service.patch_core_profile(
            user_id=uid,
            patcher=lambda current: _patch_avatar_state(
                current,
                analysis=None,
                insight=None,
                remove=True,
            ),
            expected_generation=profile_generation,
        )
    except Exception as exc:
        if getattr(exc, "code", "") == "profile_generation_changed":
            return False
        raise
    return True


def build_inspect_current_user_avatar_tool(profile_service: Any, user_id: str) -> Any:
    from ..agent.tool_registry import AgentTool

    uid = str(user_id or "").strip()

    async def _handler() -> str:
        if not bool(
            profile_service.is_enabled()
            if callable(getattr(profile_service, "is_enabled", None))
            else True
        ):
            return build_avatar_evidence_envelope().to_json()
        state = get_user_avatar_state(
            profile_service,
            uid,
            include_hash=False,
            include_hash_short=False,
        )
        return build_avatar_evidence_envelope(state.get("insight", {})).to_json()

    return AgentTool(
        name="inspect_current_user_avatar",
        description=(
            "读取当前发送者头像中可观察到的画面描述和可能的 ACG 角色/作品候选。"
            "当对方询问你能否看到或理解自己的头像，或当前对话确实涉及头像类型、审美或角色候选时调用；"
            "普通闲聊、身份或性格判断不要调用。画中人物或角色不等于发送者本人，结果不提供 URL、图片或真人身份。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_handler,
        local=True,
        enabled=lambda: bool(
            profile_service.is_enabled()
            if callable(getattr(profile_service, "is_enabled", None))
            else True
        ),
        metadata={
            "intent_tags": ["avatar", "lookup", "memory", "runtime_capability", "current_user"],
            "evidence_kind": "profile",
            "latency_class": "fast",
            "risk_level": "low",
            "side_effect": "none",
            "final_behavior": "constrained_persona_output",
            "retryable": True,
            "source_kind": "first_party_runtime",
        },
    )


def add_current_user_avatar_planner_metadata(
    available_tools: list[dict[str, Any]],
    profile_service: Any,
    user_id: str,
) -> list[dict[str, Any]]:
    uid = str(user_id or "").strip()
    items = [dict(item) for item in list(available_tools or []) if isinstance(item, dict)]
    if (
        profile_service is None
        or not uid
        or (
            callable(getattr(profile_service, "is_enabled", None))
            and not profile_service.is_enabled()
        )
    ):
        return items
    from ..agent.runtime.tool_catalog import tool_planner_metadata

    metadata = tool_planner_metadata(build_inspect_current_user_avatar_tool(profile_service, uid))
    return [item for item in items if str(item.get("name", "") or "") != metadata["name"]] + [metadata]


def register_current_user_avatar_tool(registry: Any, profile_service: Any, user_id: str) -> None:
    if registry is None or profile_service is None or not str(user_id or "").strip():
        return
    registry.register(build_inspect_current_user_avatar_tool(profile_service, str(user_id)))


__all__ = [
    "AVATAR_ALLOWED_MIMES",
    "AVATAR_ANALYSIS_SCHEMA_VERSION",
    "AVATAR_FAILURE_COOLDOWN_SECONDS",
    "AVATAR_MAX_BYTES",
    "AVATAR_SUCCESS_CHECK_INTERVAL_SECONDS",
    "AVATAR_VISION_TIMEOUT_SECONDS",
    "analyze_user_avatar",
    "add_current_user_avatar_planner_metadata",
    "build_avatar_evidence_envelope",
    "build_inspect_current_user_avatar_tool",
    "clear_user_avatar_analysis",
    "force_user_avatar_analysis",
    "get_user_avatar_state",
    "normalize_avatar_insight",
    "register_current_user_avatar_tool",
    "render_avatar_persona_context",
    "schedule_user_avatar_analysis",
    "serialize_avatar_state",
]
