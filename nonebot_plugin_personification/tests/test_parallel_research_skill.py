from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ._loader import load_personification_module

load_personification_module("plugin.personification.agent.tool_registry")
parallel_impl = load_personification_module("plugin.personification.skills.skillpacks.parallel_research.scripts.impl")
parallel_main = load_personification_module("plugin.personification.skills.skillpacks.parallel_research.scripts.main")


class _Logger:
    def debug(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def info(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    def warning(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        return None


class _FakeResponse:
    def __init__(self, content: str, tool_calls=None) -> None:  # noqa: ANN001
        self.content = content
        self.tool_calls = list(tool_calls or [])


class _FakeToolCaller:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
        self.calls.append({"messages": messages, "tools": tools, "use_builtin_search": use_builtin_search})
        system_text = str(messages[0]["content"])
        if "任务规划器" in system_text:
            return _FakeResponse(
                json.dumps(
                    {
                        "workers": [
                            {
                                "role": "visual_reference",
                                "goal": "找视觉参考",
                                "focus": ["visual"],
                                "preferred_tools": ["search_images", "generate_image"],
                            },
                            {
                                "role": "canon_setting",
                                "goal": "查设定资料",
                                "focus": ["facts"],
                                "preferred_tools": ["wiki_lookup"],
                            },
                        ],
                        "reason": "needs references",
                    },
                    ensure_ascii=False,
                )
            )
        if "只读研究子Agent" in system_text:
            await asyncio.sleep(0.01)
            user_payload = json.loads(messages[1]["content"])
            role = user_payload["role"]
            return _FakeResponse(
                json.dumps(
                    {
                        "role": role,
                        "goal": user_payload["goal"],
                        "findings": [f"{role} finding"],
                        "facts": [f"{role} fact"],
                        "visual_refs": [f"{role} visual"],
                        "prompt_hints": [f"{role} hint"],
                        "must_include": [f"{role} include"],
                        "must_avoid": [],
                        "source_notes": [f"{role} source"],
                        "confidence": "medium",
                    },
                    ensure_ascii=False,
                )
            )
        if "结果聚合器" in system_text:
            return _FakeResponse(
                json.dumps(
                    {
                        "summary": "聚合完成",
                        "facts": ["设定事实"],
                        "visual_refs": ["视觉参考"],
                        "prompt_hints": ["绘图提示"],
                        "must_include": ["必须包含"],
                        "must_avoid": ["避免错误"],
                        "source_notes": ["来源说明"],
                        "confidence": "high",
                    },
                    ensure_ascii=False,
                )
            )
        return _FakeResponse("")

    def build_tool_result_message(self, tool_call_id: str, tool_name: str, result: str) -> dict[str, str]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result,
        }


def _runtime(tool_caller=None, **config_overrides):  # noqa: ANN001, ANN003
    config = SimpleNamespace(
        personification_parallel_research_enabled=True,
        personification_parallel_research_lookup_enabled=True,
        personification_parallel_research_max_workers=6,
        personification_parallel_research_worker_timeout=20,
        personification_parallel_research_total_timeout=30,
        personification_parallel_research_max_tool_rounds=1,
        personification_tool_web_search_enabled=True,
        personification_tool_web_search_mode="enabled",
        personification_wiki_enabled=True,
        personification_wiki_fandom_enabled=True,
        personification_fandom_wikis=None,
        personification_github_token="",
        personification_fallback_enabled=False,
        personification_vision_fallback_enabled=False,
    )
    for key, value in config_overrides.items():
        setattr(config, key, value)
    return SimpleNamespace(
        plugin_config=config,
        logger=_Logger(),
        get_now=lambda: None,
        tool_caller=tool_caller or _FakeToolCaller(),
    )


def test_parallel_research_runs_dynamic_workers_and_returns_json_summary() -> None:
    caller = _FakeToolCaller()
    result = asyncio.run(
        parallel_impl.parallel_research(
            runtime=_runtime(caller),
            query="画一个角色宣传海报",
            purpose="image_generation",
        )
    )

    assert "<parallel_research_json>" in result
    assert "摘要：聚合完成" in result
    assert "设定事实" in result
    planner_calls = [call for call in caller.calls if not call["tools"]]
    worker_calls = [call for call in caller.calls if call["tools"]]
    assert len(planner_calls) == 2
    assert len(worker_calls) == 2
    for call in worker_calls:
        names = {tool["function"]["name"] for tool in call["tools"]}
        assert "generate_image" not in names


def test_parallel_research_truncates_planner_workers_to_limit() -> None:
    data = {
        "workers": [
            {"role": f"r{index}", "goal": f"goal {index}", "preferred_tools": ["web_search"]}
            for index in range(10)
        ]
    }

    plans = parallel_impl._normalize_worker_plans(
        data,
        query="test",
        purpose="lookup",
        focus=[],
        max_workers=6,
    )

    assert len(plans) == 6
    assert plans[-1].role == "r5"


def test_parallel_research_max_workers_zero_skips_llm_calls() -> None:
    caller = _FakeToolCaller()
    result = asyncio.run(
        parallel_impl.parallel_research(
            runtime=_runtime(caller),
            query="不需要研究",
            purpose="lookup",
            max_workers=0,
        )
    )

    assert caller.calls == []
    assert "max_workers_zero" in result


def test_parallel_research_tool_respects_lookup_switch() -> None:
    runtime = _runtime(personification_parallel_research_lookup_enabled=False)
    tool = parallel_main.build_tools(runtime)[0]

    result = asyncio.run(tool.handler("查资料", purpose="lookup"))

    assert "lookup_disabled_by_config" in result
