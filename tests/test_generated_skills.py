from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import yaml

from ._loader import load_personification_module


def test_tool_registry_replace_all_preserves_identity() -> None:
    mod = load_personification_module("plugin.personification.agent.tool_registry")

    async def _handler(**_kwargs):
        return "ok"

    registry = mod.ToolRegistry()
    registry.register(mod.AgentTool("old", "old", {}, _handler))
    identity = id(registry)
    version = registry.version
    registry.replace_all([mod.AgentTool("new", "new", {}, _handler)], expected_version=version)

    assert id(registry) == identity
    assert registry.get("old") is None
    assert registry.get("new") is not None
    assert registry.version == version + 1


def test_generated_skill_loads_and_composes_read_only_tool(tmp_path: Path) -> None:
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    generated = load_personification_module("plugin.personification.core.generated_skills")
    runtime_mod = load_personification_module("plugin.personification.skill_runtime.runtime_api")

    async def _search(query: str) -> str:
        return f"evidence:{query}"

    base_registry = registry_mod.ToolRegistry()
    base_registry.register(
        registry_mod.AgentTool(
            "web_search",
            "search",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            _search,
            metadata={"side_effect": "none"},
        )
    )
    skill_dir = tmp_path / "generated_skills" / "patch_notes"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "composed_tool/v1",
                "name": "patch_notes",
                "description": "search patch notes",
                "parameters": {"type": "object", "properties": {"game": {"type": "string"}}, "required": ["game"]},
                "execution": {"allowed_tools": ["web_search"], "prompt": "Find reliable patch notes.", "max_steps": 2},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    class _Caller:
        def __init__(self) -> None:
            self.calls = 0

        async def chat_with_tools(self, _messages, _tools, _builtin):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(content="", tool_calls=[SimpleNamespace(name="web_search", arguments={"query": "game patch"}, id="1")])
            return SimpleNamespace(content="final answer", tool_calls=[])

        def build_tool_result_message(self, call_id, tool_name, result):
            return {"role": "tool", "tool_call_id": call_id, "name": tool_name, "content": result}

    caller = _Caller()
    runtime = runtime_mod.SkillRuntime(
        plugin_config=SimpleNamespace(),
        logger=SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None),
        get_now=lambda: None,
        data_dir=tmp_path,
        tool_caller=caller,
    )
    assert generated.register_generated_skills(registry=base_registry, runtime=runtime) == 1
    tool = base_registry.get("patch_notes")
    assert tool is not None
    assert asyncio.run(tool.handler(game="demo")) == "final answer"


def test_generated_skill_rejects_side_effect_dependency(tmp_path: Path) -> None:
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    generated = load_personification_module("plugin.personification.core.generated_skills")
    runtime_mod = load_personification_module("plugin.personification.skill_runtime.runtime_api")

    async def _send(**_kwargs):
        return "sent"

    registry = registry_mod.ToolRegistry()
    registry.register(registry_mod.AgentTool("send", "send", {}, _send, metadata={"side_effect": "send_message"}))
    manifest = generated.validate_generated_manifest(
        {
            "kind": "composed_tool/v1",
            "name": "unsafe_composer",
            "description": "unsafe",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execution": {"allowed_tools": ["send"], "prompt": "send"},
        }
    )
    runtime = runtime_mod.SkillRuntime(
        plugin_config=SimpleNamespace(),
        logger=SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None),
        get_now=lambda: None,
        data_dir=tmp_path,
        tool_caller=SimpleNamespace(),
    )
    tool = generated.build_generated_tool(manifest, registry=registry, runtime=runtime)
    assert "cannot compose side-effect tool" in asyncio.run(tool.handler())
