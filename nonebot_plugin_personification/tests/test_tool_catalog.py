from __future__ import annotations

from ._loader import load_personification_module

tool_catalog = load_personification_module("plugin.personification.agent.runtime.tool_catalog")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")


async def _noop_handler(**_kwargs):  # noqa: ANN001
    return "ok"


def _register(registry, name: str) -> None:  # noqa: ANN001
    registry.register(
        tool_registry.AgentTool(
            name=name,
            description="",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_noop_handler,
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
