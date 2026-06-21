from __future__ import annotations

from types import SimpleNamespace

from ._loader import load_personification_module

# 复用 smoke 的 fixture + 登录
from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401


class _FakeResp:
    def __init__(self, content="ok", finish_reason="stop", raw=None):
        self.content = content
        self.finish_reason = finish_reason
        self.raw = raw
        self.tool_calls = []
        self.usage = {}
        self.model_used = "m"
        self.vision_unavailable = False


class _FakeCaller:
    def __init__(self, content="hello"):
        self._content = content

    async def chat_with_tools(self, messages, tools, use_builtin_search):
        return _FakeResp(content=self._content)


def _patch_ai_routes(monkeypatch, providers, caller_content="hi"):
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: providers)
    monkeypatch.setattr(
        ai_routes, "build_single_provider_caller",
        lambda pc, prov, **kw: _FakeCaller(content=f"{prov.get('name')}:{caller_content}"),
    )


def test_chat_all_probes_every_provider(_runtime_context, monkeypatch) -> None:
    providers = [
        {"name": "main", "api_type": "openai", "model": "gpt-4o", "priority": 1},
        {"name": "backup", "api_type": "anthropic", "model": "claude", "priority": 2},
    ]
    _patch_ai_routes(monkeypatch, providers)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 2
    names = {r["name"] for r in body["results"]}
    assert names == {"main", "backup"}
    for r in body["results"]:
        assert r["ok"] is True
        assert r["duration_ms"] >= 0
        assert r["content"].endswith(":hi")


def test_chat_all_flags_blocked_provider(_runtime_context, monkeypatch) -> None:
    providers = [{"name": "g", "api_type": "gemini", "model": "gemini-2", "priority": 1}]
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda pc, lg: providers)

    class _BlockedCaller:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            return _FakeResp(content="", raw={"candidates": [{"finishReason": "SAFETY"}]})

    monkeypatch.setattr(ai_routes, "build_single_provider_caller", lambda pc, prov, **kw: _BlockedCaller())
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": "hi"})
    assert res.status_code == 200
    r = res.json()["results"][0]
    assert r["ok"] is False
    assert "SAFETY" in r["blocked_reason"]


def test_chat_all_requires_prompt(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post("/personification/api/test/chat-all", json={"prompt": ""})
    assert res.status_code == 400


def test_persona_prompt_inline_system_prompt(_runtime_context) -> None:
    _runtime_context.plugin_config.personification_system_prompt = "你是一个活泼的群友" * 10
    _runtime_context.plugin_config.personification_prompt_path = ""
    _runtime_context.plugin_config.personification_system_path = ""
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt")
    assert res.status_code == 200, res.text
    body = res.json()
    assert "活泼的群友" in body["content"]


def test_persona_prompt_reads_specified_path(_runtime_context, tmp_path) -> None:
    f = tmp_path / "persona.txt"
    f.write_text("自定义人设内容", encoding="utf-8")
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt", params={"path": str(f)})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["is_file"] is True
    assert body["content"] == "自定义人设内容"


def test_persona_prompt_missing_path_reports_not_exists(_runtime_context, tmp_path) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.get("/personification/api/test/persona-prompt", params={"path": str(tmp_path / "nope.txt")})
    assert res.status_code == 200
    assert res.json()["exists"] is False
