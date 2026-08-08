from __future__ import annotations

import pytest

from ._loader import load_personification_module


renderer_module = load_personification_module("plugin.personification.core.social_surface_renderer")
reaction = load_personification_module(
    "plugin.personification.handlers.reply_pipeline.reaction"
)


def test_qzone_projection_extracts_complete_persona_without_chat_or_xml_fields() -> None:
    persona_yaml = """---
name: 绪山真寻
status: 静态聊天状态不应进入空间
input: <think>群聊 XML 模板</think>
system: |-
  你是绪山真寻本人。

  === 轻量工具约束（对用户不可见）===
  你首先是群聊成员，并输出 <message>。
persona_profile:
  identity_rules:
    - 怕生少女与懒散游戏脑的反差
  style_rules:
    - 短句但语义完整
  boundary_rules:
    - 群聊时决定是否接话
qzone_style:
  grounding: 具体经历必须有事件依据
  rhythm:
    - 像手机上随手敲的一句
    - 不写散文旁白
"""
    renderer = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(
            renderer_module.OutputKind.PERSONA_TEXT,
            renderer_module.PersonaScope.QZONE,
        )
    )

    projected = renderer.project_persona(persona_yaml, renderer_module.PersonaScope.QZONE)

    assert "角色名：绪山真寻" in projected
    assert "你是绪山真寻本人" in projected
    assert "怕生少女与懒散游戏脑的反差" in projected
    assert "短句但语义完整" in projected
    assert "QQ 空间专属表达" in projected
    assert "具体经历必须有事件依据" in projected
    assert "像手机上随手敲的一句；不写散文旁白" in projected
    assert renderer_module.AGENT_GUIDANCE_MARKER not in projected
    assert "你首先是群聊成员" not in projected
    assert "群聊时决定是否接话" not in projected
    assert "静态聊天状态不应进入空间" not in projected
    assert "群聊 XML 模板" not in projected
    assert "<think>" not in projected
    assert "<message>" not in projected


def test_non_chat_projection_strips_agent_guidance_but_chat_keeps_it() -> None:
    persona = "你是小满。\n\n=== 轻量工具约束（对用户不可见）===\n你首先是群聊成员。"
    renderer = renderer_module.SocialSurfaceRenderer()

    chat = renderer.project_persona(persona, renderer_module.PersonaScope.CHAT)
    private_social = renderer.project_persona(persona, renderer_module.PersonaScope.PRIVATE_SOCIAL)

    assert renderer_module.AGENT_GUIDANCE_MARKER in chat
    assert "你首先是群聊成员" in chat
    assert private_social == "你是小满。"


def test_prepare_messages_injects_persona_once_and_only_for_persona_text() -> None:
    persona = {
        "name": "小满",
        "system": "你是小满。",
        "persona_profile": {"style_rules": ["短句、自然"]},
    }
    source = [{"role": "user", "content": "说句话"}]
    persona_renderer = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(
            renderer_module.OutputKind.PERSONA_TEXT,
            renderer_module.PersonaScope.PRIVATE_SOCIAL,
        )
    )

    prepared = persona_renderer.prepare_messages(source, prompt_data=persona)
    prepared_again = persona_renderer.prepare_messages(prepared, prompt_data=persona)

    projected = persona_renderer.project_persona(persona)
    assert source == [{"role": "user", "content": "说句话"}]
    persona_systems = [
        str(item.get("content", ""))
        for item in prepared_again
        if item.get("role") == "system" and projected in str(item.get("content", ""))
    ]
    assert len(persona_systems) == 1
    assert renderer_module.PROMPT_INJECTION_GUARD_MARKER in persona_systems[0]
    assert sum(
        renderer_module.PROMPT_INJECTION_GUARD_MARKER in str(item.get("content", ""))
        for item in prepared_again
    ) == 1

    structured_renderer = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(
            renderer_module.OutputKind.STRUCTURED_DECISION,
            renderer_module.PersonaScope.PRIVATE_SOCIAL,
        )
    )
    structured = structured_renderer.prepare_messages(source, prompt_data=persona)
    assert structured[-1] == source[0]
    assert renderer_module.PROMPT_INJECTION_GUARD_MARKER in structured[0]["content"]


def test_structured_and_admin_outputs_bypass_visible_guard(monkeypatch) -> None:  # noqa: ANN001
    calls: list[str] = []

    def _guard(text, **_kwargs):  # noqa: ANN001
        calls.append(str(text))
        return "rewritten"

    monkeypatch.setattr(renderer_module, "guard_visible_text", _guard)
    raw_json = '  {"reason":"provider 安全策略拦截"}\n'
    structured = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(renderer_module.OutputKind.STRUCTURED_DECISION)
    )
    diagnostic = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(renderer_module.OutputKind.ADMIN_DIAGNOSTIC)
    )

    assert structured.finalize_text(raw_json) == raw_json
    assert diagnostic.finalize_text("provider internal server error") == "provider internal server error"
    assert calls == []


def test_verbatim_output_runs_safety_without_rewriting_safe_content() -> None:
    renderer = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(renderer_module.OutputKind.VERBATIM_USER_CONTENT)
    )

    safe = "  用户原文，保留两侧空格  "
    assert renderer.finalize_text(safe, surface="verbatim_user_content") == safe
    assert renderer.finalize_text(
        "provider 安全策略拦截",
        surface="verbatim_user_content",
    ) == ""


def test_visible_persona_and_direct_action_outputs_use_visible_guard(monkeypatch) -> None:  # noqa: ANN001
    calls: list[tuple[str, str]] = []

    def _guard(text, *, logger=None, surface="", **_kwargs):  # noqa: ANN001
        _ = logger
        calls.append((str(text), surface))
        return "guarded"

    monkeypatch.setattr(renderer_module, "guard_visible_text", _guard)
    persona = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(renderer_module.OutputKind.PERSONA_TEXT)
    )
    action = renderer_module.SocialSurfaceRenderer(
        renderer_module.SurfaceSpec(renderer_module.OutputKind.DIRECT_ACTION)
    )

    assert persona.finalize_text("普通回复", surface="chat") == "guarded"
    assert action.finalize_text("直接动作", surface="direct_action") == "guarded"
    assert calls == [("普通回复", "chat"), ("直接动作", "direct_action")]


def test_surface_registry_infers_known_specs_and_rejects_unknown_surface() -> None:
    name, spec = renderer_module.resolve_surface_spec("qzone_post")
    assert name == "qzone_post"
    assert spec.output_kind is renderer_module.OutputKind.STRUCTURED_DECISION
    assert spec.persona_scope is renderer_module.PersonaScope.QZONE
    assert renderer_module.infer_surface_name("", "social_topic_extract") == "social_topic_extract"
    assert renderer_module.infer_surface_name("", "unregistered_trigger") == renderer_module.DEFAULT_SURFACE

    with pytest.raises(ValueError, match="unsupported social surface"):
        renderer_module.resolve_surface_spec("unknown_surface")


def test_silence_reaction_consumes_structured_mood_not_message_keywords() -> None:
    class _FirstChoice:
        @staticmethod
        def choice(values):  # noqa: ANN001, ANN205
            return values[0]

    assert reaction.pick_face_id("搞笑|接梗", rng=_FirstChoice()) == 182
    assert reaction.pick_face_id("赞同|表达赞同", rng=_FirstChoice()) == 76
    assert reaction.pick_face_id("哈哈笑死", rng=_FirstChoice()) == 76
