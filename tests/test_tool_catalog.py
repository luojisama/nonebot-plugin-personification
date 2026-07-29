from __future__ import annotations

from ._loader import load_personification_module

tool_catalog = load_personification_module("plugin.personification.agent.runtime.tool_catalog")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")


async def _noop_handler(**_kwargs):  # noqa: ANN001
    return "ok"


def _register(registry, name: str, metadata: dict | None = None) -> None:  # noqa: ANN001
    registry.register(
        tool_registry.AgentTool(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_noop_handler,
            metadata=metadata or {},
        )
    )


def test_select_tool_schemas_filters_admin_tools_but_keeps_real_image_tools() -> None:
    registry = tool_registry.ToolRegistry()
    for name in ("search_web", "sticker_labeler", "user_tasks", "vision_caller", "vision_analyze"):
        _register(registry, name)

    schemas = tool_catalog.select_tool_schemas(
        registry,
        has_images=True,
        chat_intent="lookup",
        plugin_question_intent="capability",
    )
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert "search_web" in names
    assert "vision_analyze" in names
    assert "sticker_labeler" not in names
    assert "user_tasks" not in names
    assert "vision_caller" not in names


def test_select_tool_schemas_exposes_image_generation_planning_tools_for_generation_intent() -> None:
    registry = tool_registry.ToolRegistry()
    for name in ("generate_image", "parallel_research", "search_web", "search_images", "collect_resources", "vision_analyze"):
        _register(registry, name)

    schemas = tool_catalog.select_tool_schemas(
        registry,
        has_images=False,
        chat_intent="image_generation",
        plugin_question_intent="",
    )
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert names == {"generate_image", "parallel_research", "search_web", "search_images", "collect_resources"}


def test_select_tool_schemas_adds_visual_context_tools_for_image_generation_with_images() -> None:
    registry = tool_registry.ToolRegistry()
    for name in ("generate_image", "parallel_research", "search_web", "vision_analyze", "analyze_image"):
        _register(registry, name)

    schemas = tool_catalog.select_tool_schemas(
        registry,
        has_images=True,
        chat_intent="image_generation",
        plugin_question_intent="",
    )
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert names == {"generate_image", "parallel_research", "search_web", "vision_analyze", "analyze_image"}


def test_select_tool_schemas_exposes_parallel_research_for_lookup() -> None:
    registry = tool_registry.ToolRegistry()
    for name in ("parallel_research", "search_web", "vision_analyze"):
        _register(registry, name)

    schemas = tool_catalog.select_tool_schemas(
        registry,
        has_images=False,
        chat_intent="lookup",
        plugin_question_intent="",
    )
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert "parallel_research" in names
    assert "search_web" in names
    assert "vision_analyze" not in names


def test_select_tool_schemas_uses_metadata_for_new_lookup_tools() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "new_research_tool", {"intent_tags": ["lookup"], "requires_network": True})
    _register(registry, "new_admin_tool", {"intent_tags": ["lookup"], "risk_level": "admin"})

    schemas = tool_catalog.select_tool_schemas(
        registry,
        has_images=False,
        chat_intent="lookup",
        plugin_question_intent="",
    )
    names = {tool_catalog.schema_tool_name(schema) for schema in schemas}

    assert "new_research_tool" in names
    assert "new_admin_tool" not in names


def test_runtime_capability_exposes_only_safe_first_party_runtime_tools() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "list_plugins")
    _register(
        registry,
        "inspect_current_user_avatar",
        {
            "intent_tags": ["runtime_capability", "current_user"],
            "source_kind": "first_party_runtime",
            "risk_level": "low",
            "side_effect": "none",
        },
    )
    _register(
        registry,
        "untrusted_runtime_probe",
        {
            "intent_tags": ["runtime_capability"],
            "source_kind": "remote_mcp",
            "side_effect": "none",
        },
    )
    _register(
        registry,
        "runtime_write_probe",
        {
            "intent_tags": ["runtime_capability"],
            "source_kind": "first_party_runtime",
            "side_effect": "external",
        },
    )
    _register(
        registry,
        "external_image_probe",
        {
            "requires_image": True,
            "source_kind": "remote_mcp",
            "side_effect": "external",
        },
    )

    runtime_names = {
        tool_catalog.schema_tool_name(schema)
        for schema in tool_catalog.select_tool_schemas(
            registry,
            has_images=False,
            chat_intent="plugin_question",
            plugin_question_intent="runtime_capability",
        )
    }
    static_capability_names = {
        tool_catalog.schema_tool_name(schema)
        for schema in tool_catalog.select_tool_schemas(
            registry,
            has_images=False,
            chat_intent="plugin_question",
            plugin_question_intent="capability",
        )
    }
    runtime_names_with_images = {
        tool_catalog.schema_tool_name(schema)
        for schema in tool_catalog.select_tool_schemas(
            registry,
            has_images=True,
            chat_intent="plugin_question",
            plugin_question_intent="runtime_capability",
        )
    }

    assert runtime_names == {"inspect_current_user_avatar"}
    assert runtime_names_with_images == {"inspect_current_user_avatar"}
    assert static_capability_names == {"list_plugins"}


def test_conversation_action_is_visible_in_chat_profiles_but_not_runtime_capability() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "recall_latest_own_output",
        {
            "intent_tags": ["conversation_action", "runtime_capability"],
            "side_effect": "message_recall",
            "final_behavior": "silence_on_success",
            "ack_behavior": "suppress",
        },
    )

    def _selected(chat_intent: str, plugin_question_intent: str = "") -> set[str]:
        return {
            tool_catalog.schema_tool_name(schema)
            for schema in tool_catalog.select_tool_schemas(
                registry,
                has_images=False,
                chat_intent=chat_intent,
                plugin_question_intent=plugin_question_intent,
            )
        }

    assert "recall_latest_own_output" in _selected("banter")
    assert "recall_latest_own_output" in _selected("expression")
    assert "recall_latest_own_output" in _selected("explanation")
    assert "recall_latest_own_output" not in _selected("plugin_question", "runtime_capability")

    planner_metadata = tool_catalog.registry_planner_metadata(registry)[0]
    assert planner_metadata["ack_behavior"] == "suppress"


def test_registry_planner_metadata_applies_name_defaults() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "parallel_research")
    _register(registry, "vision_analyze")

    by_name = {item["name"]: item for item in tool_catalog.registry_planner_metadata(registry)}

    assert by_name["parallel_research"]["requires_network"] is True
    assert "lookup" in by_name["parallel_research"]["intent_tags"]
    assert by_name["parallel_research"]["retryable"] is True
    assert by_name["parallel_research"]["side_effect"] == "none"
    assert by_name["parallel_research"]["final_behavior"] == "continue"
    assert by_name["parallel_research"]["ack_behavior"] == "send"
    assert by_name["vision_analyze"]["requires_image"] is True
    assert "vision" in by_name["vision_analyze"]["intent_tags"]


def test_action_tool_metadata_declares_side_effect_contract() -> None:
    registry = tool_registry.ToolRegistry()
    _register(registry, "send_local_sticker")
    _register(registry, "search_and_send_images")
    _register(registry, "send_qq_face")

    by_name = {item["name"]: item for item in tool_catalog.registry_planner_metadata(registry)}

    for name in ("send_local_sticker", "search_and_send_images", "send_qq_face"):
        assert by_name[name]["evidence_kind"] == "action"
        assert by_name[name]["side_effect"] == "send_message"
        assert by_name[name]["final_behavior"] == "silence_on_success"
        assert by_name[name]["retryable"] is False


def test_runtime_metadata_merges_custom_tool_contract() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "custom_send_tool",
        {
            "intent_tags": ["expression"],
            "side_effect": "send_message",
            "final_behavior": "silence_on_success",
        },
    )

    metadata = tool_catalog.tool_runtime_metadata(registry, "custom_send_tool")

    assert "expression" in metadata["intent_tags"]
    assert metadata["side_effect"] == "send_message"
    assert metadata["final_behavior"] == "silence_on_success"


def test_select_tool_schemas_banter_exposes_lightweight_lookup_tools() -> None:
    """闲聊场景也放行 web_search/resolve_acg_entity 等轻量查证工具（不再 return []）。"""
    registry = tool_registry.ToolRegistry()
    for name in (
        "web_search",
        "resolve_acg_entity",
        "wiki_lookup",
        "weather",
        "recall_user_memory",
        "recall_group_memory",
        "memory_recall",
        "get_user_persona",
        "inspect_group_user_avatar_pair",
        "vision_analyze",
        "sticker_labeler",
    ):
        _register(registry, name)

    # 无图：放行查证工具，但不含 image-required 与 admin
    names_noimg = {
        tool_catalog.schema_tool_name(s)
        for s in tool_catalog.select_tool_schemas(registry, has_images=False, chat_intent="banter")
    }
    assert {
        "web_search",
        "resolve_acg_entity",
        "wiki_lookup",
        "weather",
        "recall_user_memory",
        "recall_group_memory",
        "memory_recall",
        "get_user_persona",
    } <= names_noimg
    assert "vision_analyze" not in names_noimg
    assert "sticker_labeler" not in names_noimg

    # 有图：在查证工具之上再叠加视觉工具
    names_img = {
        tool_catalog.schema_tool_name(s)
        for s in tool_catalog.select_tool_schemas(registry, has_images=True, chat_intent="banter")
    }
    assert "web_search" in names_img and "vision_analyze" in names_img
    assert "inspect_group_user_avatar_pair" in names_noimg


def test_select_tool_schemas_banter_exposes_builtin_game_slang_research() -> None:
    registry = tool_registry.ToolRegistry()
    _register(
        registry,
        "research_game_slang",
        {
            "source_kind": "mcp_builtin",
            "intent_tags": ["lookup", "game_slang", "social_research"],
            "risk_level": "low",
            "side_effect": "none",
            "requires_network": True,
        },
    )

    names = {
        tool_catalog.schema_tool_name(schema)
        for schema in tool_catalog.select_tool_schemas(
            registry,
            has_images=False,
            chat_intent="banter",
        )
    }

    assert "research_game_slang" in names


def test_semantic_tool_guidance_requires_lookup_for_unknown_entities() -> None:
    guidance = tool_catalog.semantic_tool_guidance()

    assert "必须先调用合适工具查证" in guidance
    assert "不要凭记忆猜" in guidance
    assert "不要直接在群里问" in guidance
    assert "专有名词" in guidance
    assert "resolve_acg_entity" in guidance
    assert "research_game_slang" in guidance
    assert "只读社交平台" in guidance
    assert "inspect_group_user_avatar_pair" in guidance
    assert "runtime_capability" in guidance
    assert "绝不表示两位用户现实中是情侣、朋友、认识或同一人" in guidance
