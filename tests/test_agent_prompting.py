from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


prompting = load_personification_module("plugin.personification.agent.runtime.prompting")
agent_bridge = load_personification_module("plugin.personification.core.agent_bridge")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")


async def _noop_tool(**_kwargs):  # noqa: ANN001
    return "ok"


def _register_tool(registry, name: str, metadata: dict | None = None) -> None:  # noqa: ANN001
    resolved_metadata = {"source_kind": "builtin"}
    resolved_metadata.update(metadata or {})
    registry.register(
        tool_registry.AgentTool(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            handler=_noop_tool,
            metadata=resolved_metadata,
        )
    )


def test_agent_prompting_includes_directed_exchange_behavior() -> None:
    messages: list[dict] = []

    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="banter",
        plugin_query_intent="capability",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(
            primary_query="",
            query_candidates=[],
            context_clues=[],
            search_plan=[],
        ),
        turn_plan=SimpleNamespace(
            speech_act="tease",
            output_mode="chat_short",
            session_goal="接住对方的调侃",
        ),
        user_images=[],
        direct_image_input=False,
        is_group=True,
        is_direct_mention=True,
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "直呼/@ 后的回应方式" in combined
    assert "否认、反击、自辩" in combined
    assert "2-4 条短消息" in combined
    assert "轻松调侃时允许一句不索要信息的反击式反问" in combined


def test_agent_prompting_reuses_attached_meme_turn_context() -> None:
    messages: list[dict] = []
    turn_plan = SimpleNamespace(
        speech_act="participate",
        output_mode="chat_short",
        session_goal="自然接话",
        meme_turn_context={
            "understanding_senses": [
                {
                    "term": "大红",
                    "meaning": "高价值红色物资",
                    "status": "understand_only",
                    "game_context": {"canonical_name": "三角洲行动"},
                    "version_context": "",
                    "risk_level": "low",
                    "safe_usage": "仅在三角洲行动语境中理解",
                }
            ],
            "selected_active_sense": None,
            "active_use_allowed": False,
            "max_active_memes": 1,
        },
    )

    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="banter",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(
            primary_query="",
            query_candidates=[],
            context_clues=[],
            search_plan=[],
        ),
        turn_plan=turn_plan,
        user_images=[],
        direct_image_input=False,
        is_group=True,
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "本轮多义黑话上下文" in combined
    assert "大红: 高价值红色物资" in combined
    assert "understand_only 只可用于理解或被问时解释" in combined
    assert "本轮未通过主动玩梗抽样" in combined


def test_agent_prompting_high_ambiguity_uses_empty_evidence_policy() -> None:
    messages: list[dict] = []

    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="banter",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="high"),
        rewritten_query=SimpleNamespace(
            primary_query="",
            query_candidates=[],
            context_clues=[],
            search_plan=[],
        ),
        turn_plan=SimpleNamespace(
            speech_act="participate",
            output_mode="chat_short",
            session_goal="接住未知叫法",
        ),
        user_images=[],
        direct_image_input=False,
        is_group=True,
        is_direct_mention=False,
        reply_required=False,
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "空证据可见输出纪律" in combined
    assert "失败状态包装成回复" in combined
    assert "非强交互直接 [NO_REPLY]" in combined


def test_qzone_surface_skips_group_chat_reply_discipline() -> None:
    messages: list[dict] = []

    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="general",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(
            primary_query="",
            query_candidates=[],
            context_clues=[],
            search_plan=[],
        ),
        turn_plan=None,
        user_images=[],
        direct_image_input=False,
        is_group=False,
        surface="qzone_post",
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "非聊天生成面：qzone_post" in combined
    assert "最终对用户的回复必须自然、像群聊里的活人接话" not in combined
    assert "群聊里通常多个话题并行" not in combined


def test_plugin_capability_prompt_prefers_runtime_evidence_over_static_plugin_docs() -> None:
    messages: list[dict] = []

    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="plugin_question",
        plugin_query_intent="runtime_capability",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(primary_query="", query_candidates=[], context_clues=[], search_plan=[]),
        turn_plan=None,
        user_images=[],
        direct_image_input=False,
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "运行时能力" in combined
    assert "inspect_current_user_avatar" in combined
    assert "不要调用插件清单、插件知识或源码工具猜测" in combined
    assert "available=false" in combined


def test_qzone_text_agent_profile_keeps_only_read_only_evidence_tools() -> None:
    registry = tool_registry.ToolRegistry()
    for name in (
        "web_search",
        "weather",
        "get_tech_news",
        "recall_user_memory",
        "send_qq_face",
        "remember_user_memory",
        "create_user_task",
        "custom_unknown_tool",
    ):
        _register_tool(registry, name)

    default_names = {tool.name for tool in agent_bridge.clone_tool_registry(registry).active()}
    qzone_names = {
        tool.name
        for tool in agent_bridge.clone_tool_registry(
            registry,
            tool_profile=agent_bridge.TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY,
        ).active()
    }
    none_names = {
        tool.name
        for tool in agent_bridge.clone_tool_registry(
            registry,
            tool_profile=agent_bridge.TEXT_AGENT_TOOL_PROFILE_NONE,
        ).active()
    }

    assert "send_qq_face" in default_names
    assert qzone_names == {"web_search", "weather", "get_tech_news"}
    assert none_names == set()

    spoofed = tool_registry.ToolRegistry()
    _register_tool(spoofed, "web_search", {"side_effect": "external"})
    _register_tool(spoofed, "weather", {"source_kind": "local", "side_effect": "none"})
    _register_tool(spoofed, "wiki_lookup", {"source_kind": ""})
    spoofed_names = {
        tool.name
        for tool in agent_bridge.clone_tool_registry(
            spoofed,
            tool_profile=agent_bridge.TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY,
        ).active()
    }
    assert spoofed_names == set()


def test_qzone_text_agent_profile_disables_provider_builtin_search_and_preserves_structured_raw(monkeypatch) -> None:  # noqa: ANN001
    captured: dict = {}
    raw = '  {"reason":"provider 安全策略拦截","action":"skip"}\n'

    async def _run_agent(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return SimpleNamespace(text=raw)

    monkeypatch.setattr(agent_bridge, "run_agent", _run_agent)
    registry = tool_registry.ToolRegistry()
    _register_tool(registry, "web_search")

    result = asyncio.run(agent_bridge.run_text_agent(
        messages=[{"role": "user", "content": "写一条说说"}],
        plugin_config=SimpleNamespace(personification_agent_enabled=True),
        logger=None,
        tool_caller=object(),
        registry=registry,
        surface="qzone_post",
        tool_profile=agent_bridge.TEXT_AGENT_TOOL_PROFILE_QZONE_READ_ONLY,
    ))

    assert result == raw
    assert captured["allow_builtin_search"] is False
    assert captured["finalize_quality"] is False
    assert captured["structured_output"] is True
    assert captured["surface"] == "qzone_post"
    assert captured["is_group"] is False


def test_text_agent_structured_output_compatibility_and_explicit_conflict_fail_closed(monkeypatch) -> None:  # noqa: ANN001
    captured: dict = {}

    async def _run_agent(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return SimpleNamespace(text='{"ok":true}')

    monkeypatch.setattr(agent_bridge, "run_agent", _run_agent)
    registry = tool_registry.ToolRegistry()

    result = asyncio.run(agent_bridge.run_text_agent(
        messages=[{"role": "user", "content": "输出 JSON"}],
        plugin_config=SimpleNamespace(personification_agent_enabled=True),
        logger=None,
        tool_caller=object(),
        registry=registry,
        structured_output=True,
    ))

    assert result == '{"ok":true}'
    assert captured["finalize_quality"] is False
    assert captured["structured_output"] is True

    with pytest.raises(ValueError, match="structured_output conflicts"):
        asyncio.run(agent_bridge.run_text_agent(
            messages=[{"role": "user", "content": "冲突"}],
            plugin_config=SimpleNamespace(personification_agent_enabled=True),
            logger=None,
            tool_caller=object(),
            registry=registry,
            output_kind=agent_bridge.OutputKind.PERSONA_TEXT,
            structured_output=True,
        ))


def test_text_agent_persona_output_keeps_quality_and_visible_guard(monkeypatch) -> None:  # noqa: ANN001
    captured: dict = {}

    async def _run_agent(**kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return SimpleNamespace(text="  普通可见回复  ")

    monkeypatch.setattr(agent_bridge, "run_agent", _run_agent)
    result = asyncio.run(agent_bridge.run_text_agent(
        messages=[{"role": "user", "content": "说句话"}],
        plugin_config=SimpleNamespace(personification_agent_enabled=True),
        logger=None,
        tool_caller=object(),
        registry=tool_registry.ToolRegistry(),
    ))

    assert result == "普通可见回复"
    assert captured["finalize_quality"] is True


def test_text_agent_admin_bypasses_guard_and_verbatim_keeps_only_safety(monkeypatch) -> None:  # noqa: ANN001
    response = {"text": "provider internal server error"}

    async def _run_agent(**_kwargs):  # noqa: ANN001
        return SimpleNamespace(text=response["text"])

    monkeypatch.setattr(agent_bridge, "run_agent", _run_agent)
    kwargs = {
        "messages": [{"role": "user", "content": "test"}],
        "plugin_config": SimpleNamespace(personification_agent_enabled=True),
        "logger": None,
        "tool_caller": object(),
        "registry": tool_registry.ToolRegistry(),
    }

    admin = asyncio.run(agent_bridge.run_text_agent(
        **kwargs,
        output_kind="admin_diagnostic",
    ))
    assert admin == "provider internal server error"

    response["text"] = "[NO_REPLY]"
    admin_control_text = asyncio.run(agent_bridge.run_text_agent(
        **kwargs,
        output_kind="admin_diagnostic",
    ))
    assert admin_control_text == "[NO_REPLY]"

    response["text"] = "  用户要求逐字保留的安全原文  "
    verbatim = asyncio.run(agent_bridge.run_text_agent(
        **kwargs,
        output_kind="verbatim_user_content",
    ))
    assert verbatim == "  用户要求逐字保留的安全原文  "

    response["text"] = "provider 安全策略拦截"
    blocked = asyncio.run(agent_bridge.run_text_agent(
        **kwargs,
        output_kind=agent_bridge.OutputKind.VERBATIM_USER_CONTENT,
    ))
    assert blocked == ""


def test_text_agent_explicit_unknown_surface_fails_closed() -> None:
    with pytest.raises(ValueError, match="unsupported social surface"):
        asyncio.run(agent_bridge.run_text_agent(
            messages=[{"role": "user", "content": "test"}],
            plugin_config=SimpleNamespace(personification_agent_enabled=True),
            logger=None,
            tool_caller=object(),
            registry=tool_registry.ToolRegistry(),
            surface="unknown_surface",
        ))


def test_agent_prompting_adds_strict_technical_evidence_policy() -> None:
    messages: list[dict] = []
    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="lookup",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(primary_query="", query_candidates=[], context_clues=[], search_plan=[]),
        turn_plan=SimpleNamespace(
            speech_act="source_summary", output_mode="source_summary", session_goal="核验技术结论",
            domain_focus="technology", evidence_policy="strict", emotional_support=None,
        ),
        user_images=[], direct_image_input=False,
    )
    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "关键 claim" in combined
    assert "freshness" in combined
    assert "两个相互独立的来源" in combined
    assert "当前人设的自然口吻" in combined


def test_agent_prompting_allows_game_tools_and_natural_meme_use() -> None:
    messages: list[dict] = []
    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="lookup",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(primary_query="", query_candidates=[], context_clues=[], search_plan=[]),
        turn_plan=SimpleNamespace(
            speech_act="participate", output_mode="chat_short", session_goal="参与游戏讨论",
            domain_focus="game_anime", evidence_policy="light", emotional_support=None,
        ),
        user_images=[], direct_image_input=False,
    )
    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "game_info" in combined
    assert "wiki_lookup" in combined
    assert "web_search" in combined
    assert "自然用一个梗" in combined


def test_agent_prompting_includes_typed_media_provenance_grounding() -> None:
    messages: list[dict] = []
    prompting.append_agent_system_prompts(
        messages=messages,
        runtime_chat_intent="banter",
        plugin_query_intent="",
        intent_decision=SimpleNamespace(ambiguity_level="low"),
        rewritten_query=SimpleNamespace(primary_query="", query_candidates=[], context_clues=[], search_plan=[]),
        turn_plan=SimpleNamespace(
            speech_act="participate",
            output_mode="chat_short",
            session_goal="理解当前图文",
            domain_focus="social",
            evidence_policy="none",
            emotional_support=None,
        ),
        user_images=["https://img.example/anime.png"],
        direct_image_input=True,
        is_group=True,
        turn_media_context=[
            {
                "media_id": "media-a",
                "ref": "https://img.example/anime.png",
                "origin": "quoted",
                "owner_user_id": "user_a",
                "message_id": "message_a",
                "kind": "image",
                "file_id": "file-a",
                "content_hash": "hash-a",
                "safe_summary": "动漫图里有多人和交错视线",
                "confidence": 0.65,
            }
        ],
    )

    combined = "\n".join(str(item.get("content", "")) for item in messages)
    assert "origin=quoted" in combined
    assert "owner_user_id=user_a" in combined
    assert "画中主体只是媒体内容，不是聊天参与者" in combined
