from __future__ import annotations

import re
import time
from typing import Any, Callable

from ...core.context_policy import strip_response_control_markers
from ...core.evidence_envelope import EvidenceEnvelope
from ...core.metrics import record_counter, record_timing
from ...core.reply_text_policy import (
    looks_like_formulaic_reply_tic,
    looks_like_markdown_reply,
    looks_like_question_reply,
    normalize_visible_reply_text,
)
from ...core.response_review import (
    is_agent_reply_ooc,
    resolve_uncertain_visible_reply,
    rewrite_agent_reply_ooc,
)
from ...core.social_surface_renderer import SocialSurfaceRenderer
from ...core.visible_output import assess_visible_text
from .final_synthesis import AgentResult


_CONTROL_REPLIES = frozenset({"[NO_REPLY]", "<NO_REPLY>", "[SILENCE]", "<SILENCE>"})
_REVISION_FLAGS = frozenset(
    {"formulaic_tic", "style_risk", "group_visible_question", "evidence_unavailable"}
)


def _is_control_reply(text: str) -> bool:
    return str(text or "").strip() in _CONTROL_REPLIES


def _is_direct_media_reply(text: str) -> bool:
    value = str(text or "").strip()
    return value.startswith("[IMAGE_B64]") and value.endswith("[/IMAGE_B64]")


def _turn_plan_output_mode(turn_plan: Any) -> str:
    return str(getattr(turn_plan, "output_mode", "") or "").strip() or "chat_short"


def _persona_system_from_messages(messages: list[dict[str, Any]]) -> str:
    for message in list(messages or []):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = str(message.get("content", "") or "").strip()
        if content:
            return content
    return ""


def _copy_result_with_quality(
    result: AgentResult,
    *,
    text: str,
    check: dict[str, Any],
) -> AgentResult:
    checks = list(getattr(result, "quality_checks", []) or [])
    checks.append(check)
    quality_context = str(getattr(result, "quality_context", "") or "")
    suppress_reply_recovery = bool(getattr(result, "suppress_reply_recovery", False))
    if quality_context == "evidence_unavailable" and _is_control_reply(text):
        suppress_reply_recovery = True
    return AgentResult(
        text=text,
        pending_actions=list(getattr(result, "pending_actions", []) or []),
        direct_output=bool(getattr(result, "direct_output", False)),
        bypass_length_limits=bool(getattr(result, "bypass_length_limits", False)),
        quality_checks=checks,
        failure_code=str(getattr(result, "failure_code", "") or ""),
        suppress_reply_recovery=suppress_reply_recovery,
        quality_context=quality_context,
        evidence_envelope=getattr(result, "evidence_envelope", None),
        social_evidence=list(getattr(result, "social_evidence", []) or []),
        social_coverage=dict(getattr(result, "social_coverage", {}) or {}),
        evidence_delivery_required=bool(getattr(result, "evidence_delivery_required", False)),
        evidence_delivery_status=str(getattr(result, "evidence_delivery_status", "not_required") or "not_required"),
        evidence_recovered=bool(getattr(result, "evidence_recovered", False)),
    )


_SOCIAL_PLATFORM_LABELS = {
    "bilibili": "B站",
    "douyin": "抖音",
    "tieba": "贴吧",
    "xiaoheihe": "小黑盒",
}


def _distinct_social_sources(sources: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    seen_urls: set[str] = set()
    for source in list(sources or []):
        if not isinstance(source, dict):
            continue
        url = str(source.get("canonical_url") or "").strip()
        group_id = str(source.get("source_group_id") or url).strip()
        if not url or url in seen_urls or group_id in seen_groups:
            continue
        seen_urls.add(url)
        seen_groups.add(group_id)
        selected.append(source)
        if len(selected) >= limit:
            break
    return selected


def _safe_social_source_line(source: dict[str, Any]) -> str:
    platform = str(source.get("platform") or "").strip().lower()
    label = _SOCIAL_PLATFORM_LABELS.get(platform, platform or "社交平台")
    url = str(source.get("canonical_url") or "").strip()
    title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip()[:120]
    if title:
        decision = assess_visible_text(
            title,
            allow_control=False,
            allow_direct_media=False,
            enforce_role_integrity=True,
        )
        if decision.allowed:
            return f"{title}（{label}）：{url}"
    return f"{label}：{url}"


def finalize_social_evidence_delivery(
    result: AgentResult,
    *,
    sources: list[dict[str, Any]],
    coverage: dict[str, Any] | None = None,
    partial: bool = False,
    warnings: list[str] | None = None,
    record_trace: Callable[..., None] | None = None,
) -> AgentResult:
    """Enforce the visible source-link contract for validated social MCP data."""

    result.social_evidence = list(sources or [])[:10]
    result.social_coverage = {
        **dict(coverage or {}),
        "partial": bool(partial),
        "warnings": list(warnings or [])[:8],
    }
    result.evidence_delivery_required = bool(
        result.evidence_delivery_required
        or result.social_evidence
        or int(result.social_coverage.get("returned_count", 0) or 0) > 0
    )
    if not result.evidence_delivery_required:
        result.evidence_delivery_status = "not_required"
        return result

    if not result.social_evidence:
        result.text = "[NO_REPLY]"
        result.evidence_delivery_status = "failed"
        result.failure_code = "evidence_delivery_incomplete"
        if record_trace is not None:
            record_trace(
                key="agent_evidence_delivery",
                label="Agent 证据交付",
                status="error",
                detail="status=failed diagnostic=evidence_delivery_incomplete links=0",
            )
        return result

    current = str(getattr(result, "text", "") or "").strip()
    valid_urls = [str(source.get("canonical_url") or "") for source in result.social_evidence]
    current_visibility = assess_visible_text(current)
    if current_visibility.allowed and any(url and url in current for url in valid_urls):
        result.evidence_delivery_status = "met"
        if record_trace is not None:
            record_trace(
                key="agent_evidence_delivery",
                label="Agent 证据交付",
                status="ok",
                detail=(
                    f"status=met links={sum(1 for url in valid_urls if url and url in current)} "
                    f"groups={int(result.social_coverage.get('source_group_count', 0) or 0)}"
                ),
            )
        return result

    selected = _distinct_social_sources(result.social_evidence, limit=3)
    lines = [_safe_social_source_line(source) for source in selected]
    safe_base = current if current_visibility.allowed and current not in _CONTROL_REPLIES else ""
    fallback = "\n".join([safe_base, "来源：" if safe_base else "查到的来源：", *lines]).strip()
    fallback_visibility = assess_visible_text(
        fallback,
        allow_control=False,
        allow_direct_media=False,
    )
    if not fallback_visibility.allowed:
        fallback = "\n".join(
            ["查到的来源：", *(str(source.get("canonical_url") or "") for source in selected)]
        ).strip()
        fallback_visibility = assess_visible_text(
            fallback,
            allow_control=False,
            allow_direct_media=False,
        )
    if fallback_visibility.allowed and any(url and url in fallback for url in valid_urls):
        result.text = fallback_visibility.text
        result.evidence_delivery_status = "recovered"
        result.evidence_recovered = True
        result.failure_code = ""
        if record_trace is not None:
            record_trace(
                key="agent_evidence_delivery",
                label="Agent 证据交付",
                status="warn",
                detail=(
                    "status=recovered diagnostic=visible_output_recovered "
                    f"model_visible={str(current_visibility.allowed).lower()} "
                    f"pattern_id={current_visibility.pattern_id or '-'} links={len(selected)}"
                ),
                hint="主回复未保留来源或被可见输出规则拦截，已改用经过校验的结构化来源。",
            )
        return result

    result.text = "[NO_REPLY]"
    result.evidence_delivery_status = "failed"
    result.failure_code = "evidence_delivery_incomplete"
    if record_trace is not None:
        record_trace(
            key="agent_evidence_delivery",
            label="Agent 证据交付",
            status="error",
            detail="status=failed diagnostic=evidence_delivery_incomplete links=0",
        )
    return result


def _looks_like_group_context(messages: list[dict[str, Any]], turn_plan: Any = None) -> bool:
    for message in list(messages or []):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        content = str(message.get("content", "") or "")
        if any(marker in content for marker in ("群聊", "群里", "群友", "群成员")):
            return True
    target = str(getattr(turn_plan, "message_target", "") or "").strip()
    return target in {"broadcast", "someone_else", "uncertain"}


def _quality_flags(
    raw_text: str,
    visible_text: str,
    *,
    is_group: bool = False,
    allow_rhetorical_banter: bool = False,
) -> list[str]:
    flags: list[str] = []
    if looks_like_markdown_reply(raw_text):
        flags.append("markdown_or_trace")
    if looks_like_formulaic_reply_tic(raw_text):
        flags.append("formulaic_tic")
    if is_agent_reply_ooc(raw_text):
        flags.append("style_risk")
    if is_group and looks_like_question_reply(
        visible_text or raw_text,
        allow_exclamatory_rhetorical=allow_rhetorical_banter,
    ):
        flags.append("group_visible_question")
    if visible_text != str(raw_text or "").strip():
        flags.append("normalized")
    if not visible_text:
        flags.append("empty_after_normalize")
    return flags


async def finalize_agent_reply_quality(
    result: AgentResult,
    *,
    tool_caller: Any,
    messages: list[dict[str, Any]],
    turn_plan: Any = None,
    is_group: bool | None = None,
    is_direct_mention: bool = False,
    reply_required: bool = False,
    current_user_text: str = "",
    record_trace: Callable[..., None] | None = None,
    logger: Any = None,
    reason: str = "",
) -> AgentResult:
    """Run one final output-style quality pass for Agent text.

    This is deliberately an output hygiene layer: it normalizes visible text,
    detects assistant/OOC/formulaic surface patterns, and optionally asks the
    model for one rewrite. It does not decide user intent, emotion, or whether
    a normal chat turn should be routed to a feature.
    """

    started_at = time.monotonic()
    raw_text = str(getattr(result, "text", "") or "").strip()
    quality_context = str(getattr(result, "quality_context", "") or "").strip()
    direct_output = bool(getattr(result, "direct_output", False))
    envelope = EvidenceEnvelope.from_value(getattr(result, "evidence_envelope", None))
    if quality_context == "constrained_persona_output" and envelope is not None:
        outcome = await SocialSurfaceRenderer().render_evidence(
            envelope,
            tool_caller=tool_caller,
            persona_system=_persona_system_from_messages(messages),
        )
        final_text = normalize_visible_reply_text(outcome.text) or envelope.natural_fallback
        group_context = _looks_like_group_context(messages, turn_plan) if is_group is None else bool(is_group)
        constraint_flags = ["constrained_evidence"]
        if is_agent_reply_ooc(final_text):
            final_text = envelope.natural_fallback
            constraint_flags.append("style_fallback")
        if group_context and looks_like_question_reply(final_text):
            final_text = envelope.natural_fallback
            constraint_flags.append("question_fallback")
        visibility = assess_visible_text(final_text)
        if not visibility.allowed:
            final_text = envelope.natural_fallback
            constraint_flags.append("visible_output_fallback")
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        check = {
            "action": outcome.action,
            "reason": str(reason or ""),
            "flags": constraint_flags,
            "revision_attempted": outcome.rewrite_used,
            "elapsed_ms": elapsed_ms,
            "original_chars": len(raw_text),
            "final_chars": len(final_text),
        }
        record_timing("agent.reply_quality_ms", elapsed_ms, action=outcome.action)
        record_counter("agent.reply_quality_total", action=outcome.action)
        if record_trace is not None:
            record_trace(
                key="agent_reply_quality",
                label="Agent 回复质量",
                status="ok" if outcome.action in {"accepted", "rewritten"} else "warn",
                detail=(
                    f"action={outcome.action} source={reason or '-'} "
                    f"flags={','.join(constraint_flags)} revision={str(outcome.rewrite_used).lower()} "
                    f"elapsed_ms={elapsed_ms} chars={len(raw_text)}->{len(final_text)}"
                ),
                hint="头像等受约束证据已完成人设化与事实边界审阅。",
            )
        return _copy_result_with_quality(result, text=final_text, check=check)
    if quality_context != "evidence_unavailable":
        visibility = assess_visible_text(raw_text)
        if not visibility.allowed:
            check = {
                "action": "silenced",
                "reason": visibility.reason,
                "pattern_id": visibility.pattern_id,
                "summary_hash": visibility.summary_hash,
                "source": str(reason or ""),
                "flags": ["unsafe_visible_output"],
                "revision_attempted": False,
                "elapsed_ms": int((time.monotonic() - started_at) * 1000),
                "original_chars": len(raw_text),
                "final_chars": len("[SILENCE]"),
            }
            record_counter("agent.reply_quality_total", action="silenced")
            if record_trace is not None:
                record_trace(
                    key="agent_reply_quality",
                    label="Agent 回复质量",
                    status="warn",
                    detail=(
                        f"action=silenced reason={visibility.reason} "
                        f"pattern_id={visibility.pattern_id or '-'} chars={len(raw_text)} "
                        f"summary_hash={visibility.summary_hash or '-'} flags=unsafe_visible_output"
                    ),
                )
            return _copy_result_with_quality(result, text="[SILENCE]", check=check)
    skipped = (
        direct_output
        or _is_direct_media_reply(raw_text)
        or (_is_control_reply(raw_text) and quality_context != "evidence_unavailable")
    )
    if skipped:
        check = {
            "action": "skipped",
            "reason": "direct_or_control",
            "source": str(reason or ""),
            "flags": [],
            "revision_attempted": False,
            "elapsed_ms": int((time.monotonic() - started_at) * 1000),
            "original_chars": len(raw_text),
            "final_chars": len(raw_text),
        }
        if record_trace is not None:
            record_trace(
                key="agent_reply_quality",
                label="Agent 回复质量",
                status="info",
                detail=(
                    f"action=skipped reason=direct_or_control source={reason or '-'} "
                    f"flags=- chars={len(raw_text)}"
                ),
            )
        record_counter("agent.reply_quality_total", action="skipped")
        return _copy_result_with_quality(result, text=raw_text, check=check)

    if quality_context == "evidence_unavailable":
        group_context = _looks_like_group_context(messages, turn_plan) if is_group is None else bool(is_group)

        async def _call_uncertain_review(review_messages: list[dict[str, Any]]) -> str:
            if tool_caller is None:
                return ""
            response = await tool_caller.chat_with_tools(review_messages, [], False)
            return str(getattr(response, "content", "") or "")

        decision = await resolve_uncertain_visible_reply(
            _call_uncertain_review,
            candidate_text=raw_text,
            raw_message_text=current_user_text,
            persona_system=_persona_system_from_messages(messages),
            turn_plan=turn_plan,
            reply_required=reply_required,
            is_private=not group_context,
            evidence_unavailable=True,
            timeout=8.0,
        )
        final_text = "[SILENCE]"
        action = (
            "no_evidence_silenced"
            if decision.reason == "no_evidence_nonrequired"
            else "context_request_rejected"
        )
        if decision.action == "request_context" and decision.text:
            candidate = normalize_visible_reply_text(strip_response_control_markers(decision.text))
            candidate_visibility = assess_visible_text(candidate)
            invalid_group_question = bool(
                group_context
                and looks_like_question_reply(
                    candidate,
                    allow_exclamatory_rhetorical=False,
                )
            )
            if candidate and candidate_visibility.allowed and not invalid_group_question:
                final_text = candidate
                action = "context_request"
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        flags = list(dict.fromkeys(["evidence_unavailable", *decision.flags]))
        check = {
            "action": action,
            "flags": flags,
            "elapsed_ms": elapsed_ms,
        }
        record_timing("agent.reply_quality_ms", elapsed_ms, action=action)
        record_counter("agent.reply_quality_total", action=action)
        if record_trace is not None:
            record_trace(
                key="agent_reply_quality",
                label="Agent 回复质量",
                status="ok" if action == "context_request" else "warn",
                detail=(
                    f"action={action} flags={','.join(flags)} "
                    f"elapsed_ms={elapsed_ms}"
                ),
                hint="空证据不再包装成可见失败说明；强交互仅允许经过语义复核的具体补充请求。",
            )
        return _copy_result_with_quality(result, text=final_text, check=check)

    stripped = strip_response_control_markers(raw_text)
    visible_text = normalize_visible_reply_text(stripped)
    group_context = _looks_like_group_context(messages, turn_plan) if is_group is None else bool(is_group)
    speech_act = str(getattr(turn_plan, "speech_act", "") or "").strip()
    allow_rhetorical_banter = bool(
        group_context
        and is_direct_mention
        and speech_act in {"", "participate", "tease"}
    )
    flags = _quality_flags(
        raw_text,
        visible_text,
        is_group=group_context,
        allow_rhetorical_banter=allow_rhetorical_banter,
    )
    if quality_context == "evidence_unavailable":
        flags.append("evidence_unavailable")
    action = "accept"
    final_text = visible_text or raw_text
    revision_attempted = False

    if flags and tool_caller is not None and any(flag in _REVISION_FLAGS for flag in flags):
        revision_attempted = True
        rewritten = await rewrite_agent_reply_ooc(
            tool_caller=tool_caller,
            original_text=raw_text,
            persona_system=_persona_system_from_messages(messages),
            timeout=8.0,
            output_mode=_turn_plan_output_mode(turn_plan),
            avoid_questions=group_context,
            allow_rhetorical_banter=allow_rhetorical_banter,
            rewrite_reason=quality_context,
        )
        candidate = normalize_visible_reply_text(strip_response_control_markers(rewritten)) if rewritten else ""
        if candidate:
            if group_context and looks_like_question_reply(
                candidate,
                allow_exclamatory_rhetorical=allow_rhetorical_banter,
            ):
                final_text = "[SILENCE]"
                action = "silenced"
            else:
                final_text = candidate
                action = "rewritten"

    if not final_text:
        final_text = "[SILENCE]"
        action = "silenced"
    elif group_context and "group_visible_question" in flags and action != "rewritten":
        final_text = "[SILENCE]"
        action = "silenced"
    elif quality_context == "evidence_unavailable" and action != "rewritten":
        final_text = "[SILENCE]"
        action = "silenced"
    elif flags and is_agent_reply_ooc(final_text):
        final_text = "[SILENCE]"
        action = "silenced"
    elif flags and action != "rewritten":
        action = "normalized" if final_text != raw_text else "accepted_with_flags"

    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    flags_text = ",".join(flags) if flags else "-"
    record_timing("agent.reply_quality_ms", elapsed_ms, action=action)
    record_counter("agent.reply_quality_total", action=action)
    check = {
        "action": action,
        "reason": str(reason or ""),
        "flags": flags,
        "revision_attempted": revision_attempted,
        "elapsed_ms": elapsed_ms,
        "original_chars": len(raw_text),
        "final_chars": len(final_text),
    }
    if record_trace is not None:
        status = "ok" if action in {"accept", "normalized", "skipped"} else "warn"
        record_trace(
            key="agent_reply_quality",
            label="Agent 回复质量",
            status=status,
            detail=(
                f"action={action} source={reason or '-'} flags={flags_text} "
                f"revision={str(revision_attempted).lower()} elapsed_ms={elapsed_ms} "
                f"chars={len(raw_text)}->{len(final_text)}"
            ),
            hint=(
                "命中输出风格风险后已做一次修订或静默；这只处理可见文本风格，不替代对话语义判断"
                if flags
                else ""
            ),
        )
    if logger is not None and flags:
        try:
            logger.debug(f"[agent] reply quality action={action} flags={flags_text}")
        except Exception:
            pass
    return _copy_result_with_quality(result, text=final_text, check=check)


__all__ = [
    "finalize_agent_reply_quality",
    "finalize_social_evidence_delivery",
]
