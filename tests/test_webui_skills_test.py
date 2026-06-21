from __future__ import annotations

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
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path), personification_agent_max_steps=5)
    data_store_mod.init_data_store(cfg)

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
