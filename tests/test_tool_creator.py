from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.tool_calls = []


class _Caller:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    async def chat_with_tools(self, _messages, _tools, _builtin):
        return _Response(self.responses.pop(0))


def _runtime(tmp_path: Path, caller: _Caller):
    registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    registry = registry_mod.ToolRegistry()

    async def _search(query: str) -> str:
        return '{"results":[{"title":"official","url":"https://example.com","snippet":"facts"}]}'

    registry.register(
        registry_mod.AgentTool(
            "web_search",
            "search the web",
            {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            _search,
            metadata={"side_effect": "none", "risk_level": "low"},
        )
    )
    bundle = SimpleNamespace(
        tool_registry=registry,
        reply_processor_deps=SimpleNamespace(runtime=SimpleNamespace(agent_tool_caller=caller)),
    )

    def _reload():
        generated = load_personification_module("plugin.personification.core.generated_skills")
        runtime_api = load_personification_module("plugin.personification.skill_runtime.runtime_api")
        runtime_obj = runtime_api.SkillRuntime(
            plugin_config=SimpleNamespace(),
            logger=SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None),
            get_now=lambda: None,
            data_dir=tmp_path,
            tool_caller=caller,
        )
        loaded = generated.register_generated_skills(registry=registry, runtime=runtime_obj)
        assert loaded == 1

    bundle.reload_runtime_services = _reload
    return SimpleNamespace(
        plugin_config=SimpleNamespace(personification_data_dir=str(tmp_path)),
        runtime_bundle=bundle,
        logger=SimpleNamespace(info=lambda *_a: None, warning=lambda *_a: None),
    )


def _init_store(tmp_path: Path, monkeypatch) -> None:
    paths = load_personification_module("plugin.personification.core.paths")
    data_store = load_personification_module("plugin.personification.core.data_store")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))


def test_tool_creator_pauses_for_creator_answer(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    service_mod = load_personification_module("plugin.personification.core.tool_creator")
    monkeypatch.setattr(service_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    caller = _Caller(['{"action":"ask_admin","question":"只看官网吗？","reason":"来源范围影响结果","options":["是","否"]}'])
    service = service_mod.ToolCreatorService(_runtime(tmp_path, caller))
    task = service.create(creator="10001", request_text="做一个更新查询工具")
    asyncio.run(service.run(task["task_id"]))
    paused = service.get(task["task_id"])
    assert paused["status"] == "awaiting_admin"
    assert paused["question"]["prompt"] == "只看官网吗？"

    resumed = service.answer(
        task_id=task["task_id"],
        creator="10001",
        question_id=paused["question"]["question_id"],
        expected_version=paused["version"],
        answer="只看官网",
    )
    assert resumed["status"] == "queued"
    try:
        service.answer(
            task_id=task["task_id"], creator="10002", question_id="stale", expected_version=resumed["version"], answer="x"
        )
    except PermissionError:
        pass
    else:
        raise AssertionError("non-creator answer must be rejected")


def test_tool_creator_builds_approves_and_activates(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    service_mod = load_personification_module("plugin.personification.core.tool_creator")
    monkeypatch.setattr(service_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    caller = _Caller(
        [
            '{"action":"continue","research_queries":["demo official updates"],"draft":{}}',
            '{"action":"produce","manifest":{"kind":"composed_tool/v1","name":"demo_updates","description":"查询 demo 的官方更新时调用","parameters":{"type":"object","properties":{"topic":{"type":"string"}},"required":["topic"]},"execution":{"allowed_tools":["web_search"],"prompt":"只查官方更新并给出来源。","max_steps":3}}}',
        ]
    )
    runtime = _runtime(tmp_path, caller)
    service = service_mod.ToolCreatorService(runtime)
    task = service.create(creator="10001", request_text="做一个 demo 官方更新查询工具", suggested_name="demo_updates")
    asyncio.run(service.run(task["task_id"]))
    ready = service.get(task["task_id"])
    assert not ready["error"], ready["error"]
    assert ready["status"] == "ready_for_approval", ready
    assert ready["artifact_digest"]
    completed = asyncio.run(
        service.approve(
            task_id=task["task_id"],
            creator="10001",
            expected_version=ready["version"],
            artifact_digest=ready["artifact_digest"],
        )
    )
    assert completed["status"] == "completed"
    assert runtime.runtime_bundle.tool_registry.get("demo_updates") is not None


def test_tool_creator_approval_rejects_stale_digest(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    service_mod = load_personification_module("plugin.personification.core.tool_creator")
    monkeypatch.setattr(service_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    service = service_mod.ToolCreatorService(_runtime(tmp_path, _Caller([])))
    task = service.create(creator="10001", request_text="x")
    service._set(
        task["task_id"],
        status="ready_for_approval",
        phase="ready_for_approval",
        progress=95,
        context={"manifest": {"name": "demo"}},
        artifact_digest="real",
        artifact_path=str(tmp_path),
    )
    ready = service.get(task["task_id"])
    try:
        asyncio.run(service.approve(task_id=task["task_id"], creator="10001", expected_version=ready["version"], artifact_digest="stale"))
    except RuntimeError as exc:
        assert "artifact changed" in str(exc)
    else:
        raise AssertionError("stale artifact approval must fail")


def test_tool_creator_cancel_wins_over_late_model_result(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    service_mod = load_personification_module("plugin.personification.core.tool_creator")
    monkeypatch.setattr(service_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    service = service_mod.ToolCreatorService(_runtime(tmp_path, _Caller([])))
    task = service.create(creator="10001", request_text="build")
    cancelled = service.cancel(task_id=task["task_id"], creator="10001", expected_version=task["version"])
    assert cancelled["status"] == "cancelled"
    assert service._may_continue(task["task_id"]) is False


def test_tool_creator_retry_requeues_failed_task(tmp_path: Path, monkeypatch) -> None:
    _init_store(tmp_path, monkeypatch)
    service_mod = load_personification_module("plugin.personification.core.tool_creator")
    monkeypatch.setattr(service_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    service = service_mod.ToolCreatorService(_runtime(tmp_path, _Caller([])))
    task = service.create(creator="10001", request_text="build")
    service._set(task["task_id"], status="failed", phase="failed", progress=100, error="ValueError")
    failed = service.get(task["task_id"])
    retried = service.retry(task_id=task["task_id"], creator="10001", expected_version=failed["version"])
    assert retried["status"] == "queued"
    assert retried["error"] == ""
