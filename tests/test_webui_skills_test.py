from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_context(tmp_path: Path, monkeypatch):
    data_store_mod = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    tool_health = load_personification_module("plugin.personification.core.tool_health")
    tool_health.reset_tool_health_statuses()
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_agent_max_steps=5,
        personification_skill_sources=[],
        personification_skill_remote_enabled=False,
        personification_skill_require_admin_review=True,
        personification_skill_allow_unsafe_external=False,
        personification_use_skillpacks=False,
    )
    data_store_mod.init_data_store(cfg)
    review_mod = load_personification_module("plugin.personification.core.remote_skill_review")
    source_resolver = load_personification_module("plugin.personification.skill_runtime.source_resolver")

    async def _prepare_reviews(raw_sources, _cfg, logger, *, data_dir):  # noqa: ANN001
        prepared = [
            {**source, "content_digest": f"{index + 1:064x}"}
            for index, source in enumerate(source_resolver.parse_skill_sources(raw_sources, logger))
        ]
        review_mod.list_remote_skill_reviews(prepared, logger)
        return prepared

    monkeypatch.setattr(review_mod, "prepare_remote_skill_reviews", _prepare_reviews)

    tool_registry_mod = load_personification_module("plugin.personification.agent.tool_registry")
    registry = tool_registry_mod.ToolRegistry()

    async def _noop(**_kw):
        return ""

    registry.register(tool_registry_mod.AgentTool(name="web_search", description="联网搜索", parameters={}, handler=_noop, metadata={"category": "network"}))
    registry.register(tool_registry_mod.AgentTool(name="generate_image", description="生成图片", parameters={}, handler=_noop, metadata={"category": "media"}))

    class _FakeCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            tool_impl = load_personification_module("plugin.personification.skills.skillpacks.tool_caller.scripts.impl")
            return tool_impl.ToolCallerResponse(
                finish_reason="stop",
                content="OK 模拟回复",
                tool_calls=[],
                raw={"sim": True},
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                model_used="sim-model",
            )

    runtime_bundle = SimpleNamespace(
        tool_registry=registry,
        memory_store=None,
        profile_service=None,
        reply_processor_deps=SimpleNamespace(
            runtime=SimpleNamespace(agent_tool_caller=_FakeCaller()),
        ),
    )

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=runtime_bundle,
    )
    return SimpleNamespace(plugin_config=cfg, app_module=app_module, runtime_bundle=runtime_bundle, registry=registry)


def _build_client(rt):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(rt.app_module.build_router())
    return TestClient(app)


def _login(client, rt) -> None:
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    rt.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    res2 = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": code, "device_label": "t"})
    assert res2.status_code == 200, res2.text
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_skills_listing_and_toggle(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.get("/personification/api/skills")
    assert res.status_code == 200
    body = res.json()
    names = {s["name"]: s for s in body["skills"]}
    assert "web_search" in names
    assert names["web_search"]["user_disabled"] is False

    res2 = client.post(
        "/personification/api/skills/web_search/toggle",
        json={"disabled": True, "reason": "测试禁用"},
    )
    assert res2.status_code == 200, res2.text
    diagnostic = res2.json()["diagnostic"]
    assert diagnostic["code"] == "skill_disabled"
    assert diagnostic["phase"] == "override_persisted"
    assert [item["key"] for item in diagnostic["steps"]] == [
        "resolve_skill",
        "persist_override",
        "verify_override",
    ]

    res3 = client.get("/personification/api/skills")
    after = {s["name"]: s for s in res3.json()["skills"]}
    assert after["web_search"]["user_disabled"] is True

    # ToolRegistry.active() 现在应该跳过被禁的
    assert "web_search" not in {t.name for t in _runtime_context.registry.active()}


def test_toggle_unknown_skill_returns_404(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.post("/personification/api/skills/not_exist/toggle", json={"disabled": True})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "skill_not_found"


def test_skill_toggle_failure_is_structured_and_redacted(_runtime_context, monkeypatch) -> None:
    skill_overrides = load_personification_module("plugin.personification.core.skill_overrides")

    def _fail(*_args, **_kwargs):
        raise RuntimeError("password=hunter2 https://private.example/skill?token=secret")

    monkeypatch.setattr(skill_overrides, "set_disabled", _fail)
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post("/personification/api/skills/web_search/toggle", json={"disabled": True})
    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "skill_toggle_persist_failed"
    assert report["outcome_unknown"] is True
    assert report["trace_id"]
    serialized = json.dumps(report)
    assert "hunter2" not in serialized
    assert "private.example" not in serialized
    assert "secret" not in serialized


def test_skill_page_reports_remote_sources_and_mcp_tools(_runtime_context) -> None:
    mcp_mod = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    original = dict(mcp_mod._REGISTERED_MCP_TOOLS)
    mcp_mod._REGISTERED_MCP_TOOLS.clear()
    mcp_mod.register_mcp_endpoint(
        registered_name="demo_mcp_tool",
        remote_name="search",
        command="python",
        args=["server.py"],
        env={"TOKEN": "secret"},
        cwd="D:\\demo",
        timeout=7,
    )
    try:
        _runtime_context.plugin_config.personification_skill_sources = [
            {
                "name": "demo_remote",
                "source": "https://github.com/example/skillpack",
                "ref": "main",
                "subdir": "skills/demo",
            }
        ]
        _runtime_context.plugin_config.personification_skill_remote_enabled = True
        client = _build_client(_runtime_context)
        _login(client, _runtime_context)

        res = client.get("/personification/api/skills")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["summary"]["remote_enabled"] is True
        assert body["summary"]["remote_sources"] == 1
        assert body["summary"]["remote_pending"] == 1
        assert body["summary"]["mcp_tools"] == 1
        assert body["remote_sources"][0]["status"] == "pending"
        assert body["mcp_tools"][0]["name"] == "demo_mcp_tool"
        assert body["mcp_tools"][0]["env_count"] == 1
    finally:
        mcp_mod._REGISTERED_MCP_TOOLS.clear()
        mcp_mod._REGISTERED_MCP_TOOLS.update(original)


def test_skill_page_reports_tool_health_disabled(_runtime_context) -> None:
    tool_health = load_personification_module("plugin.personification.core.tool_health")
    web_search = _runtime_context.registry.get("web_search")
    assert web_search is not None
    web_search.metadata.update({"requires_network": True, "side_effect": "none"})

    result = asyncio.run(tool_health.probe_registry_tools(registry=_runtime_context.registry, timeout_seconds=1))
    assert result["disabled"] == 1

    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.get("/personification/api/skills")
    assert res.status_code == 200, res.text
    body = res.json()
    names = {s["name"]: s for s in body["skills"]}
    assert names["web_search"]["health_disabled"] is True
    assert names["web_search"]["health"]["available"] is False
    assert body["summary"]["health_disabled"] == 1
    assert body["summary"]["active"] == 1


def test_remote_skill_review_endpoint_updates_status(_runtime_context) -> None:
    _runtime_context.plugin_config.personification_skill_sources = [
        {
            "name": "demo_remote",
            "source": "https://github.com/example/skillpack",
            "ref": "main",
            "subdir": "skills/demo",
        }
    ]
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    listed = client.get("/personification/api/skills").json()
    selector = listed["remote_sources"][0]["key"]
    res = client.post(
        "/personification/api/skills/remote/review",
        json={"selector": selector, "status": "approved"},
    )
    assert res.status_code == 200, res.text
    result = res.json()
    assert result["matched_count"] == 1
    assert result["diagnostic"]["code"] == "remote_skill_review_approved"
    assert next(item for item in result["diagnostic"]["steps"] if item["key"] == "persist_review")["status"] == "ok"

    listed_after = client.get("/personification/api/skills").json()
    assert listed_after["summary"]["remote_approved"] == 1
    assert listed_after["remote_sources"][0]["status"] == "approved"
    assert len(listed_after["remote_sources"][0]["content_digest"]) == 64
    assert (
        listed_after["remote_sources"][0]["approved_digest"]
        == listed_after["remote_sources"][0]["content_digest"]
    )


def test_add_remote_skill_source_persists_config_and_auto_approves(_runtime_context, tmp_path: Path) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post(
        "/personification/api/skills/remote/source",
        json={
            "name": "demo_remote",
            "source": "https://github.com/example/skillpack/tree/main/skills/demo",
            "auto_approve": True,
            "prefer_first": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert body["auto_approved"] is True
    assert body["diagnostic"]["code"] == "remote_skill_source_added_approved"
    assert body["diagnostic"]["phase"] == "review_complete"
    steps = {item["key"]: item for item in body["diagnostic"]["steps"]}
    assert steps["parse_source"]["status"] == "ok"
    assert steps["deduplicate_source"]["status"] == "ok"
    assert steps["persist_sources"]["status"] == "ok"
    assert steps["auto_approve"]["status"] == "ok"
    assert steps["reload_runtime"]["status"] == "pending"
    assert _runtime_context.plugin_config.personification_skill_remote_enabled is True
    assert _runtime_context.plugin_config.personification_skill_allow_unsafe_external is False
    assert _runtime_context.plugin_config.personification_skill_sources[0]["name"] == "demo_remote"

    env_json = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert env_json["personification_skill_remote_enabled"] is True
    assert env_json["personification_skill_sources"][0]["source"].startswith("https://github.com/example/skillpack")
    assert env_json["personification_skill_sources"][0]["ref"] == "main"
    assert env_json["personification_skill_sources"][0]["subdir"] == "skills/demo"

    listed = client.get("/personification/api/skills").json()
    assert listed["remote_sources"][0]["status"] == "approved"


def test_remote_skill_toggle_persists_config(_runtime_context, tmp_path: Path) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post("/personification/api/skills/remote/toggle", json={"enabled": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert body["enabled"] is True
    assert body["needs_reload"] is True
    assert body["diagnostic"]["code"] == "remote_skill_loading_enabled"
    assert _runtime_context.plugin_config.personification_skill_remote_enabled is True
    env_json = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert env_json["personification_skill_remote_enabled"] is True


def test_add_duplicate_remote_skill_source_reports_noop(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    payload = {
        "name": "demo_remote",
        "source": "https://github.com/example/skillpack/tree/main/skills/demo",
        "enable_remote": False,
    }

    first = client.post("/personification/api/skills/remote/source", json=payload)
    second = client.post("/personification/api/skills/remote/source", json=payload)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["success"] is True
    assert body["changed"] is False
    assert body["diagnostic"]["code"] == "remote_skill_source_duplicate"
    dedupe_step = next(item for item in body["diagnostic"]["steps"] if item["key"] == "deduplicate_source")
    assert dedupe_step["status"] == "warn"
    assert len(_runtime_context.plugin_config.personification_skill_sources) == 1


def test_remote_skill_source_rejects_url_credentials_without_echo(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post(
        "/personification/api/skills/remote/source",
        json={"source": "https://alice:hunter2@example.com/private.zip?token=secret"},
    )
    assert res.status_code == 400, res.text
    report = res.json()["detail"]
    assert report["code"] == "remote_skill_source_credentials_forbidden"
    serialized = json.dumps(report)
    assert "alice" not in serialized
    assert "hunter2" not in serialized
    assert "example.com" not in serialized
    assert "secret" not in serialized

    query_res = client.post(
        "/personification/api/skills/remote/source",
        json={"source": "https://example.com/public.zip?token=query-secret"},
    )
    assert query_res.status_code == 400, query_res.text
    query_report = query_res.json()["detail"]
    assert query_report["code"] == "remote_skill_source_credentials_forbidden"
    query_serialized = json.dumps(query_report)
    assert "example.com" not in query_serialized
    assert "query-secret" not in query_serialized


def test_remote_skill_source_malformed_url_returns_parse_diagnostic(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post(
        "/personification/api/skills/remote/source",
        json={"source": "https://[broken-ipv6/skill.zip"},
    )
    assert res.status_code == 400, res.text
    report = res.json()["detail"]
    assert report["code"] == "remote_skill_source_invalid"
    assert report["phase"] == "source_parse"
    assert next(item for item in report["steps"] if item["key"] == "parse_source")["status"] == "error"


def test_remote_source_reports_partial_config_persistence(_runtime_context, monkeypatch) -> None:
    env_writer = load_personification_module("plugin.personification.core.env_writer")
    original_write_both = env_writer.write_both

    def _write_both(field_name, value, cfg):
        if field_name == "personification_skill_remote_enabled":
            return {
                "field_name": field_name,
                "value": value,
                "dotenv_path": None,
                "env_json_path": None,
                "errors": ["password=hunter2 https://private.example/config?token=secret"],
            }
        return original_write_both(field_name, value, cfg)

    monkeypatch.setattr(env_writer, "write_both", _write_both)
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post(
        "/personification/api/skills/remote/source",
        json={"name": "partial", "source": "https://github.com/example/partial", "enable_remote": True},
    )
    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "remote_skill_source_persistence_partial"
    assert report["partial"] is True
    assert report["outcome_unknown"] is False
    details = {item["label"]: item["value"] for item in report["details"]}
    assert "personification_skill_sources" in details["已持久化字段"]
    assert details["失败字段"] == "personification_skill_remote_enabled"
    assert len(_runtime_context.plugin_config.personification_skill_sources) == 1
    assert _runtime_context.plugin_config.personification_skill_remote_enabled is False
    serialized = json.dumps(report)
    assert "hunter2" not in serialized
    assert "private.example" not in serialized
    assert "secret" not in serialized


def test_remote_source_auto_approve_failure_reports_partial_unknown(_runtime_context, monkeypatch) -> None:
    review = load_personification_module("plugin.personification.core.remote_skill_review")

    def _fail_review(*_args, **_kwargs):
        raise RuntimeError("https://admin:password@private.example/review?token=secret")

    monkeypatch.setattr(review, "review_remote_skill_sources", _fail_review)
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post(
        "/personification/api/skills/remote/source",
        json={"name": "review_fail", "source": "https://github.com/example/review-fail", "auto_approve": True},
    )
    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "remote_skill_source_auto_approve_unknown"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert next(item for item in report["steps"] if item["key"] == "auto_approve")["status"] == "unknown"
    serialized = json.dumps(report)
    assert "password" not in serialized
    assert "private.example" not in serialized
    assert "secret" not in serialized


def test_skill_runtime_reload_endpoint_calls_bundle(_runtime_context) -> None:
    calls: list[int] = []
    _runtime_context.runtime_bundle.reload_runtime_services = lambda: calls.append(1)
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post("/personification/api/skills/reload")
    assert res.status_code == 200, res.text
    assert calls == [1]
    body = res.json()
    assert body["success"] is True
    assert body["diagnostic"]["code"] == "skill_runtime_reloaded"
    assert next(item for item in body["diagnostic"]["steps"] if item["key"] == "verify_registry")["status"] == "ok"


def test_skill_runtime_reload_failure_is_unknown_and_redacted(_runtime_context) -> None:
    def _fail_reload():
        raise RuntimeError("https://admin:password@private.example/reload?token=secret")

    _runtime_context.runtime_bundle.reload_runtime_services = _fail_reload
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)

    res = client.post("/personification/api/skills/reload")
    assert res.status_code == 500, res.text
    report = res.json()["detail"]
    assert report["code"] == "skill_runtime_reload_outcome_unknown"
    assert report["partial"] is True
    assert report["outcome_unknown"] is True
    assert report["retryable"] is False
    assert report["trace_id"]
    serialized = json.dumps(report)
    assert "password" not in serialized
    assert "private.example" not in serialized
    assert "secret" not in serialized


def test_skill_frontend_persists_and_renders_operation_diagnostics() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-tools.js").read_text(encoding="utf-8")

    assert "persistSkillOperationResult(result)" in source
    assert "operationDiagnosticFromError(e" in source
    assert "sessionStorage.setItem(_SKILL_OPERATION_RESULT_STORAGE_KEY" in source
    assert "renderOperationDiagnostic(result)" in source
    assert 'api("/skills/remote/toggle"' in source
    assert "digest=${escapeHtml((item.content_digest" in source


def test_model_test_chat_returns_response(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.post(
        "/personification/api/test/chat",
        json={"prompt": "你好", "system": "测试系统"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "OK 模拟回复" in body["content"]
    assert body["model_used"] == "sim-model"
    assert body["usage"]["prompt_tokens"] == 10
    assert body["usage"]["completion_tokens"] == 5


def test_test_chat_rejects_empty_prompt(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.post("/personification/api/test/chat", json={"prompt": "  "})
    assert res.status_code == 400


def test_recommended_defaults_returns_payload(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.get("/personification/api/config/recommended-defaults")
    assert res.status_code == 200
    defaults = res.json()["defaults"]
    assert "personification_probability" in defaults
    assert defaults["personification_agent_max_steps"] == 5


def test_apply_recommended_writes_subset(_runtime_context, tmp_path, monkeypatch) -> None:
    env_writer = load_personification_module("plugin.personification.core.env_writer")
    env_file = tmp_path / ".env.prod"
    env_file.write_text("personification_agent_max_steps=10\n", encoding="utf-8")
    monkeypatch.setattr(env_writer, "_resolve_dotenv_target", lambda field_name="": env_file)
    monkeypatch.setattr(env_writer, "read_env_file_value", lambda _k: "")

    client = _build_client(_runtime_context)
    _login(client, _runtime_context)
    res = client.post(
        "/personification/api/config/apply-recommended",
        json={"fields": ["personification_agent_max_steps", "personification_probability"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "personification_agent_max_steps" in body["applied"]
    from dotenv import dotenv_values
    parsed = dotenv_values(str(env_file))
    assert parsed["personification_agent_max_steps"] == "10"
    env_json = json.loads((tmp_path / "env.json").read_text(encoding="utf-8"))
    assert env_json["personification_agent_max_steps"] == 5
    assert env_json["personification_probability"] == 0.35
    assert _runtime_context.plugin_config.personification_agent_max_steps == 5
