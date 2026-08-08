from __future__ import annotations

import asyncio
import json
import time
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
        personification_parallel_research_pages_per_worker=20,
        personification_deep_research_v2_enabled=False,
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


def test_lookup_focus_uses_three_structured_workers_without_planner() -> None:
    caller = _FakeToolCaller()
    result = asyncio.run(
        parallel_impl.parallel_research(
            runtime=_runtime(caller),
            query="三角洲行动 花来 梗百科 黑话 由来 玩法",
            purpose="lookup",
            focus=["查定义", "查玩法", "查反证"],
            max_workers=3,
        )
    )

    system_prompts = [str(call["messages"][0]["content"]) for call in caller.calls]
    assert not any("任务规划器" in prompt for prompt in system_prompts)
    assert sum("只读研究子Agent" in prompt for prompt in system_prompts) == 3
    assert "structured_lookup_plan" in result
    assert '"research_level": "legacy:lookup"' in result
    assert '"pages_per_worker": 4' in result
    worker_calls = [call for call in caller.calls if call["tools"]]
    assert worker_calls
    for call in worker_calls:
        names = {tool["function"]["name"] for tool in call["tools"]}
        assert "web_fetch" in names


def test_lookup_limits_clamp_v2_and_legacy_to_one_absolute_reply_budget() -> None:
    limits = parallel_impl.ResearchLimits(
        max_workers=8,
        worker_timeout=180.0,
        total_timeout=300.0,
        max_tool_rounds=4,
        pages_per_worker=40,
        level="high",
    )

    bounded = parallel_impl._bounded_lookup_limits(limits)

    assert bounded.max_workers == 3
    assert bounded.worker_timeout == 28.0
    assert bounded.total_timeout == 30.0
    assert bounded.max_tool_rounds == 2
    assert bounded.pages_per_worker == 4
    assert bounded.level == "high:lookup"

    constrained = parallel_impl._bounded_lookup_limits(
        limits,
        time_budget_seconds=7.5,
    )
    assert constrained.total_timeout == 7.5
    assert constrained.worker_timeout == 7.5


def test_lookup_worker_deadline_is_distinct_from_total_deadline(monkeypatch) -> None:
    class _SlowCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            system_text = str(messages[0]["content"])
            if "只读研究子Agent" in system_text or "结果聚合器" in system_text:
                await asyncio.sleep(0.2)
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    monkeypatch.setattr(parallel_impl, "_LOOKUP_TOTAL_TIMEOUT_SECONDS", 0.20)
    monkeypatch.setattr(parallel_impl, "_LOOKUP_WORKER_TIMEOUT_SECONDS", 0.05)
    started_at = time.monotonic()
    result = asyncio.run(
        parallel_impl.parallel_research(
            runtime=_runtime(_SlowCaller()),
            query="限时查证",
            purpose="lookup",
            focus=["查定义", "查反证"],
            max_workers=2,
        )
    )

    assert time.monotonic() - started_at < 0.8
    assert "parallel_research_worker_timeout" in result
    assert "parallel_research_total_timeout" not in result
    payload = json.loads(result.split("<parallel_research_json>\n", 1)[1].split("\n</parallel_research_json>", 1)[0])
    assert payload["research_runtime"]["status"] == "timeout"
    assert payload["research_runtime"]["worker_timeout_count"] == 2
    assert payload["research_runtime"]["total_deadline_exhausted"] is False


def test_lookup_worker_timeout_preserves_completed_partial_evidence(monkeypatch) -> None:
    class _PartiallySlowCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            system_text = str(messages[0]["content"])
            if "只读研究子Agent" in system_text:
                user_payload = json.loads(messages[1]["content"])
                if user_payload["role"] == "lookup_2":
                    await asyncio.sleep(0.2)
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    monkeypatch.setattr(parallel_impl, "_LOOKUP_TOTAL_TIMEOUT_SECONDS", 0.20)
    monkeypatch.setattr(parallel_impl, "_LOOKUP_WORKER_TIMEOUT_SECONDS", 0.05)
    result = asyncio.run(
        parallel_impl.parallel_research(
            runtime=_runtime(_PartiallySlowCaller()),
            query="保留部分查证",
            purpose="lookup",
            focus=["查定义", "查反证"],
            max_workers=2,
        )
    )

    payload = json.loads(result.split("<parallel_research_json>\n", 1)[1].split("\n</parallel_research_json>", 1)[0])
    runtime = payload["research_runtime"]
    assert runtime["status"] == "partial"
    assert runtime["completed_workers"] == 1
    assert runtime["worker_timeout_count"] == 1
    assert "lookup_1 fact" in payload["facts"]


def test_lookup_deadline_does_not_wait_for_provider_cancellation(monkeypatch) -> None:
    class _CancellationSlowCaller(_FakeToolCaller):
        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001
            system_text = str(messages[0]["content"])
            if "只读研究子Agent" in system_text:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.2)
                    raise
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    async def _run() -> tuple[float, str]:
        started_at = time.monotonic()
        result = await parallel_impl.parallel_research(
            runtime=_runtime(_CancellationSlowCaller()),
            query="取消不协作的限时查证",
            purpose="lookup",
            focus=["查定义", "查玩法", "查反证"],
            max_workers=3,
        )
        elapsed = time.monotonic() - started_at
        await asyncio.sleep(0.25)
        return elapsed, result

    monkeypatch.setattr(parallel_impl, "_LOOKUP_TOTAL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(parallel_impl, "_LOOKUP_WORKER_TIMEOUT_SECONDS", 0.20)
    elapsed, result = asyncio.run(_run())

    assert elapsed < 0.15
    assert "parallel_research_total_timeout" in result
    assert "parallel_research_worker_timeout" not in result
    payload = json.loads(result.split("<parallel_research_json>\n", 1)[1].split("\n</parallel_research_json>", 1)[0])
    assert payload["research_runtime"]["status"] == "timeout"
    assert payload["research_runtime"]["total_deadline_exhausted"] is True
    assert parallel_impl._detached_research_task_snapshot()["active"] == 0


def test_detached_research_task_registry_recovers_after_cancellation_burst() -> None:
    before = parallel_impl._detached_research_task_snapshot()

    async def _run() -> None:
        async def _slow_cleanup() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.01)
                raise

        tasks = [asyncio.create_task(_slow_cleanup()) for _ in range(50)]
        await asyncio.sleep(0)
        for task in tasks:
            parallel_impl._detach_cancelled_task(task)
        assert parallel_impl._detached_research_task_snapshot()["active"] >= 50
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    after = parallel_impl._detached_research_task_snapshot()
    assert after["active"] == 0
    assert after["created_total"] - before["created_total"] == 50
    assert after["completed_total"] - before["completed_total"] == 50


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
    assert tool.parameters["properties"]["time_budget_seconds"]["maximum"] == 30
    assert tool.metadata["query_retry_limit"] == 1


def test_parallel_research_v2_high_profile_expands_limits() -> None:
    runtime = _runtime(personification_deep_research_v2_enabled=True)

    limits = parallel_impl._resolve_research_limits(
        plugin_config=runtime.plugin_config,
        max_workers=None,
        research_level="high",
    )

    assert limits.max_workers == 8
    assert limits.pages_per_worker == 40
    assert limits.total_timeout == 300.0
    assert limits.max_tool_rounds == 3


def test_parallel_research_fallback_aggregate_cross_verifies_repeated_facts() -> None:
    plans = [
        parallel_impl.ResearchWorkerPlan(role="a", goal="查 A", focus=[], preferred_tools=[]),
        parallel_impl.ResearchWorkerPlan(role="b", goal="查 B", focus=[], preferred_tools=[]),
    ]

    payload = parallel_impl._fallback_aggregate(
        query="测试",
        purpose="lookup",
        plans=plans,
        worker_results=[
            {
                "role": "a",
                "facts": ["共同事实", "单源 A"],
                "sources": ["https://example.com/a"],
                "conflicts": [],
            },
            {
                "role": "b",
                "facts": ["共同事实", "单源 B"],
                "sources": ["https://example.com/b"],
                "conflicts": ["B 与 A 的年份说法不同"],
            },
        ],
        notes=[],
    )

    assert payload["verified_facts"] == ["共同事实"]
    assert payload["single_source_facts"] == ["单源 A", "单源 B"]
    assert payload["sources"] == ["https://example.com/a", "https://example.com/b"]
    assert payload["conflicts"] == ["B 与 A 的年份说法不同"]


def test_parallel_research_fact_evidence_keeps_url_quote_mapping_and_deduplicates_mirrors() -> None:
    payload = parallel_impl._fallback_aggregate(
        query="三角洲行动 黑话",
        purpose="lookup",
        plans=[],
        worker_results=[
            {
                "role": "definition",
                "facts": ["该词描述一种玩法"],
                "fact_evidence": [
                    {
                        "claim": "该词描述一种玩法",
                        "support": [
                            {
                                "canonical_url": "https://example.com/article#part",
                                "title": "梗百科",
                                "quote": "该词描述一种保留对方护甲后夺取装备的玩法。",
                                "content_fingerprint": "a" * 64,
                            },
                            {
                                "canonical_url": "https://mirror.example.net/repost",
                                "title": "转载",
                                "quote": "该词描述一种保留对方护甲后夺取装备的玩法。",
                                "content_fingerprint": "a" * 64,
                            },
                        ],
                    }
                ],
            }
        ],
        notes=[],
    )

    fact = payload["fact_evidence"][0]
    assert fact["claim"] == "该词描述一种玩法"
    assert fact["support"][0]["canonical_url"] == "https://example.com/article"
    assert {item["source_group_id"] for item in fact["support"]} == {"web_source_" + "a" * 24}
    assert {item["evidence_origin"] for item in fact["support"]} == {
        "web:example.com",
        "web:mirror.example.net",
    }


def test_parallel_research_fact_evidence_rejects_nonpublic_or_unquoted_sources() -> None:
    assert parallel_impl._fact_evidence_items(
        [
            {"claim": "事实", "support": [{"canonical_url": "http://example.com", "quote": "有效摘录"}]},
            {"claim": "事实", "support": [{"canonical_url": "https://127.0.0.1/a", "quote": "有效摘录"}]},
            {"claim": "事实", "support": [{"canonical_url": "https://example.com/a", "quote": ""}]},
        ]
    ) == []


def test_worker_fact_evidence_requires_quote_from_fetched_page() -> None:
    fingerprint = "b" * 64
    result = parallel_impl._validated_worker_fact_evidence(
        [
            {
                "claim": "花来描述一种夺取装备的玩法",
                "support": [
                    {
                        "canonical_url": "https://example.com/article#section",
                        "title": "玩法说明",
                        "quote": "击杀后带走对方整套装备",
                    },
                    {
                        "canonical_url": "https://example.com/article",
                        "title": "伪造摘录",
                        "quote": "页面里不存在的句子",
                    },
                ],
            }
        ],
        fetched_pages={
            "https://example.com/article": {
                "title": "真实页面",
                "text": "这种玩法会在击杀后带走对方整套装备，并尽快撤离。",
                "content_fingerprint": fingerprint,
            }
        },
    )

    assert len(result) == 1
    assert len(result[0]["support"]) == 1
    assert result[0]["support"][0]["content_fingerprint"] == fingerprint
    assert result[0]["support"][0]["canonical_url"] == "https://example.com/article"


def test_worker_fact_evidence_normalizes_unicode_and_inline_cjk_spacing() -> None:
    body = "红狼开启大招后捏碎一朵“花”，并快速撤离。"
    result = parallel_impl._validated_worker_fact_evidence(
        [
            {
                "claim": "花来与红狼玩法有关",
                "support": [
                    {
                        "canonical_url": "https://example.com/article",
                        "quote": '红狼开启大招后捏碎一朵 "花"',
                    }
                ],
            }
        ],
        fetched_pages={
            "https://example.com/article": {
                "title": "真实页面",
                "text": "红狼开启大招后\n捏碎一朵“花”，并快速撤离。",
                "content_fingerprint": "c" * 64,
                "content_similarity_fingerprint": parallel_impl._content_simhash(body),
                "content_length": str(len(body)),
            }
        },
    )

    assert len(result) == 1
    assert result[0]["support"][0]["quote"] == '红狼开启大招后捏碎一朵"花"'


def test_fact_evidence_groups_near_duplicate_reposts() -> None:
    original = (
        "花来是三角洲行动中的红狼夺舍玩法。玩家使用高射速武器和低级肉伤弹攻击腿部，"
        "尽量保留对方头盔与护甲耐久，击杀后带走装备，再开启大招快速撤离。"
    )
    repost = (
        "转载说明：花来是三角洲行动里的红狼夺舍玩法。玩家使用高射速武器和低级肉伤弹攻击腿部，"
        "尽量保留对方头盔与护甲耐久，击杀之后带走装备，然后开启大招快速撤离。"
    )
    unrelated = (
        "本期介绍排位地图的出生点位和物资刷新规律，重点分析队伍站位、侦察路线、"
        "交战距离以及不同天气下的视野变化，不涉及红狼夺取装备的玩法。"
    )
    original_simhash = parallel_impl._content_simhash(original)
    repost_simhash = parallel_impl._content_simhash(repost)
    unrelated_simhash = parallel_impl._content_simhash(unrelated)
    assert (int(original_simhash, 16) ^ int(repost_simhash, 16)).bit_count() <= 12
    assert (int(original_simhash, 16) ^ int(unrelated_simhash, 16)).bit_count() > 12

    evidence = parallel_impl._fact_evidence_items(
        [
            {
                "claim": "花来是一种红狼夺舍玩法",
                "support": [
                    {
                        "canonical_url": "https://example.com/original",
                        "quote": "使用高射速武器和低级肉伤弹攻击腿部",
                        "content_fingerprint": "d" * 64,
                        "content_similarity_fingerprint": original_simhash,
                        "content_length": len(original),
                    },
                    {
                        "canonical_url": "https://mirror.example.net/repost",
                        "quote": "使用高射速武器和低级肉伤弹攻击腿部",
                        "content_fingerprint": "e" * 64,
                        "content_similarity_fingerprint": repost_simhash,
                        "content_length": len(repost),
                    },
                    {
                        "canonical_url": "https://guide.example.org/map",
                        "quote": "重点分析队伍站位、侦察路线",
                        "content_fingerprint": "f" * 64,
                        "content_similarity_fingerprint": unrelated_simhash,
                        "content_length": len(unrelated),
                    },
                ],
            }
        ]
    )

    assert len(evidence) == 1
    supports = evidence[0]["support"]
    assert supports[0]["source_group_id"] == supports[1]["source_group_id"]
    assert supports[2]["source_group_id"] != supports[0]["source_group_id"]
    assert len({item["evidence_origin"] for item in supports}) == 3


def test_web_fetch_tool_rejects_unsafe_targets_without_echoing_input(monkeypatch) -> None:
    registry = parallel_impl._build_readonly_registry(_runtime())
    tool = registry.get("web_fetch")
    assert tool is not None

    result = json.loads(asyncio.run(tool.handler(url="http://user:secret@127.0.0.1/private")))

    assert result == {"ok": False, "error_code": "web_fetch_target_rejected"}
    assert "secret" not in json.dumps(result)


def test_web_fetch_tool_validates_final_url_and_returns_stable_errors(monkeypatch) -> None:
    registry = parallel_impl._build_readonly_registry(_runtime())
    tool = registry.get("web_fetch")
    assert tool is not None

    async def _redirected_to_http(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return {
            "url": "http://example.com/downgrade",
            "status_code": 200,
            "title": "不应返回",
            "text": "正文",
        }

    monkeypatch.setattr(parallel_impl, "fetch_web_page", _redirected_to_http)
    rejected = json.loads(asyncio.run(tool.handler(url="https://example.com/start")))
    assert rejected == {"ok": False, "error_code": "web_fetch_redirect_rejected"}

    async def _known_failure(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise parallel_impl.WebFetchError("包含不应回传的内部地址和秘密")

    monkeypatch.setattr(parallel_impl, "fetch_web_page", _known_failure)
    failed = json.loads(asyncio.run(tool.handler(url="https://example.com/start")))
    assert failed == {"ok": False, "error_code": "web_fetch_rejected"}
    assert "秘密" not in json.dumps(failed, ensure_ascii=False)
