from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import yaml

from .context_policy import (
    PROMPT_INJECTION_GUARD_MARKER,
    build_prompt_injection_guard,
    ensure_prompt_injection_guard,
)
from .evidence_envelope import (
    EvidenceEnvelope,
    EvidenceRenderOutcome,
    render_constrained_evidence,
)
from .prompt_loader import AGENT_GUIDANCE_MARKER
from .persona_profile import load_persona_profile
from .visible_output import guard_visible_text


class OutputKind(str, Enum):
    PERSONA_TEXT = "persona_text"
    STRUCTURED_DECISION = "structured_decision"
    VERBATIM_USER_CONTENT = "verbatim_user_content"
    DIRECT_ACTION = "direct_action"
    ADMIN_DIAGNOSTIC = "admin_diagnostic"


class PersonaScope(str, Enum):
    CHAT = "chat"
    PRIVATE_SOCIAL = "private_social"
    GROUP_SOCIAL = "group_social"
    QZONE = "qzone"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class SurfaceSpec:
    output_kind: OutputKind
    persona_scope: PersonaScope = PersonaScope.NONE


DEFAULT_SURFACE = "agent_bridge"

# Every non-default inference is explicit. Callers may still pass output_kind when
# a single surface intentionally emits more than one kind of output.
SURFACE_REGISTRY: dict[str, SurfaceSpec] = {
    DEFAULT_SURFACE: SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.CHAT),
    "chat": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.CHAT),
    "private_social": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "group_social": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.GROUP_SOCIAL),
    "qzone_post": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.QZONE),
    "qzone_post_rewrite": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.QZONE),
    "qzone_diary": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.QZONE),
    "qzone_social_feed_action": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.QZONE),
    "qzone_comment_reply": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.QZONE),
    "proactive_private": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.PRIVATE_SOCIAL),
    "proactive_group_idle": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.GROUP_SOCIAL),
    "proactive_group_idle_mode": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_topic_extract": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_gate_topic_followup": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_gate_news_push": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_gate_festival_greeting": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_gate_morning_greeting": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_gate_evening_greeting": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "social_topic_followup": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "social_news_push": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "social_morning_greeting": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "social_evening_greeting": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "social_festival_greeting": SurfaceSpec(OutputKind.PERSONA_TEXT, PersonaScope.PRIVATE_SOCIAL),
    "structured_decision": SurfaceSpec(OutputKind.STRUCTURED_DECISION, PersonaScope.NONE),
    "verbatim_user_content": SurfaceSpec(OutputKind.VERBATIM_USER_CONTENT, PersonaScope.NONE),
    "direct_action": SurfaceSpec(OutputKind.DIRECT_ACTION, PersonaScope.NONE),
    "admin_diagnostic": SurfaceSpec(OutputKind.ADMIN_DIAGNOSTIC, PersonaScope.NONE),
}


def _coerce_output_kind(value: OutputKind | str) -> OutputKind:
    if isinstance(value, OutputKind):
        return value
    try:
        return OutputKind(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported social surface output kind: {value!r}") from exc


def _coerce_persona_scope(value: PersonaScope | str) -> PersonaScope:
    if isinstance(value, PersonaScope):
        return value
    try:
        return PersonaScope(str(value or "").strip().lower())
    except ValueError as exc:
        raise ValueError(f"unsupported social surface persona scope: {value!r}") from exc


def resolve_surface_spec(surface: str = "") -> tuple[str, SurfaceSpec]:
    name = str(surface or "").strip() or DEFAULT_SURFACE
    spec = SURFACE_REGISTRY.get(name)
    if spec is None:
        raise ValueError(f"unsupported social surface: {name}")
    return name, spec


def infer_surface_name(surface: str = "", trigger_reason: str = "") -> str:
    explicit = str(surface or "").strip()
    if explicit:
        return explicit
    inferred = str(trigger_reason or "").strip()
    if inferred in SURFACE_REGISTRY:
        return inferred
    return DEFAULT_SURFACE


def _parse_persona(prompt_data: Any) -> dict[str, Any]:
    if isinstance(prompt_data, dict):
        return dict(prompt_data)
    if not isinstance(prompt_data, str):
        return {}
    raw = prompt_data.strip()
    if not raw:
        return {}
    try:
        parsed = yaml.safe_load(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"system": raw}


def _render_rule_lines(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    if isinstance(value, dict):
        lines: list[str] = []
        for key, item in value.items():
            rendered = _render_inline(item)
            if rendered:
                lines.append(f"{key}: {rendered}")
        return lines
    text = str(value or "").strip()
    return [text] if text else []


def _render_inline(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item or "").strip())
    if isinstance(value, dict):
        return "；".join(
            f"{key}: {rendered}"
            for key, item in value.items()
            if (rendered := _render_inline(item))
        )
    return str(value or "").strip()


def _render_persona_profile(profile: Any, *, include_boundaries: bool) -> str:
    if not isinstance(profile, dict):
        return ""
    sections = (
        ("身份一致性", "identity_rules"),
        ("语气风格", "style_rules"),
        ("边界策略", "boundary_rules"),
    )
    rendered: list[str] = []
    for title, key in sections:
        if key == "boundary_rules" and not include_boundaries:
            continue
        lines = _render_rule_lines(profile.get(key))
        if lines:
            rendered.append(f"[{title}]\n" + "\n".join(f"- {line}" for line in lines))
    return "## 人设快照\n" + "\n".join(rendered) if rendered else ""


def _render_qzone_style(value: Any) -> str:
    lines = _render_rule_lines(value)
    if not lines:
        return ""
    return "## QQ 空间专属表达\n" + "\n".join(f"- {line}" for line in lines)


class SocialSurfaceRenderer:
    def __init__(self, spec: SurfaceSpec | None = None) -> None:
        self.spec = spec or SURFACE_REGISTRY[DEFAULT_SURFACE]

    @property
    def finalize_quality(self) -> bool:
        return self.spec.output_kind is OutputKind.PERSONA_TEXT

    def project_persona(
        self,
        prompt_data: Any,
        scope: PersonaScope | str | None = None,
    ) -> str:
        resolved_scope = self.spec.persona_scope if scope is None else _coerce_persona_scope(scope)
        if resolved_scope is PersonaScope.NONE:
            return ""
        persona = _parse_persona(prompt_data)
        if not persona:
            return ""
        parts: list[str] = []
        name = str(persona.get("name", "") or "").strip()
        if name:
            parts.append(f"角色名：{name}")
        system = str(
            persona.get("qzone_system", "")
            if resolved_scope is PersonaScope.QZONE and persona.get("qzone_system")
            else persona.get("system", "")
            or ""
        ).strip()
        if resolved_scope is not PersonaScope.CHAT and AGENT_GUIDANCE_MARKER in system:
            system = system.split(AGENT_GUIDANCE_MARKER, 1)[0].rstrip()
        if system:
            parts.append(system)
        profile_data = persona.get("persona_profile")
        if not isinstance(profile_data, dict) and resolved_scope is PersonaScope.QZONE:
            profile_data = load_persona_profile(prompt_data)
        profile = _render_persona_profile(
            profile_data,
            include_boundaries=resolved_scope is not PersonaScope.QZONE,
        )
        if profile:
            parts.append(profile)
        if resolved_scope is PersonaScope.QZONE:
            qzone_style = _render_qzone_style(persona.get("qzone_style"))
            if qzone_style:
                parts.append(qzone_style)
        return "\n\n".join(parts).strip()

    def prepare_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        prompt_data: Any = None,
        scope: PersonaScope | str | None = None,
        output_kind: OutputKind | str | None = None,
    ) -> list[dict[str, Any]]:
        prepared = [dict(message) for message in list(messages or [])]
        kind = self.spec.output_kind if output_kind is None else _coerce_output_kind(output_kind)
        if kind is OutputKind.PERSONA_TEXT and prompt_data is not None:
            projected = self.project_persona(prompt_data, scope)
            if projected:
                secured_projected = ensure_prompt_injection_guard(projected)
                persona_index = next(
                    (
                        index
                        for index, message in enumerate(prepared)
                        if message.get("role") == "system"
                        and str(message.get("content", "") or "").strip()
                        in {projected, secured_projected}
                    ),
                    None,
                )
                if persona_index is None:
                    prepared.insert(0, {"role": "system", "content": secured_projected})
                else:
                    prepared[persona_index]["content"] = secured_projected
        resolved_scope = self.spec.persona_scope if scope is None else _coerce_persona_scope(scope)
        if resolved_scope is not PersonaScope.NONE and not any(
            message.get("role") == "system"
            and PROMPT_INJECTION_GUARD_MARKER in str(message.get("content", "") or "")
            for message in prepared
        ):
            prepared.insert(0, {"role": "system", "content": build_prompt_injection_guard()})
        return prepared

    def finalize_text(
        self,
        text: Any,
        *,
        logger: Any = None,
        surface: str = "",
        output_kind: OutputKind | str | None = None,
    ) -> str:
        kind = self.spec.output_kind if output_kind is None else _coerce_output_kind(output_kind)
        raw = str(text or "")
        if kind in {OutputKind.STRUCTURED_DECISION, OutputKind.ADMIN_DIAGNOSTIC}:
            return raw
        if kind is OutputKind.VERBATIM_USER_CONTENT:
            safe = guard_visible_text(raw, logger=logger, surface=surface)
            return raw if safe else ""
        return guard_visible_text(
            raw,
            logger=logger,
            surface=surface,
            allow_control=False,
        )

    async def render_evidence(
        self,
        envelope: EvidenceEnvelope,
        *,
        tool_caller: Any,
        persona_system: str = "",
        timeout: float = 8.0,
    ) -> EvidenceRenderOutcome:
        return await render_constrained_evidence(
            envelope,
            tool_caller=tool_caller,
            persona_system=persona_system,
            timeout=timeout,
        )


__all__ = [
    "DEFAULT_SURFACE",
    "OutputKind",
    "PersonaScope",
    "SURFACE_REGISTRY",
    "SocialSurfaceRenderer",
    "SurfaceSpec",
    "infer_surface_name",
    "resolve_surface_spec",
]
