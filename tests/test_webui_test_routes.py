from __future__ import annotations

from pathlib import Path
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


def test_config_provider_models_probes_openai_compatible(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"id": "gpt-test"}, {"id": "gpt-test-mini"}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def get(self, url, headers=None, params=None):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _Resp()

    monkeypatch.setattr(config_routes.httpx, "AsyncClient", _Client)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/config/provider-models",
        json={
            "provider": {
                "api_type": "openai",
                "api_url": "https://example.test/v1",
                "api_key": "sk-test",
                "model": "gpt-current-alias",
            }
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert captured["url"] == "https://example.test/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert [m["id"] for m in body["models"]] == ["gpt-current-alias", "gpt-test", "gpt-test-mini"]


def test_config_provider_models_probes_gemini_openai_compatible(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")
    captured: dict = {}

    class _Resp:
        def raise_for_status(self):  # noqa: ANN201
            return None

        def json(self):  # noqa: ANN201
            return {"data": [{"id": "gemini-2.5-flash"}, {"id": "gemini-2.5-pro"}]}

    class _Client:
        def __init__(self, **kwargs):  # noqa: ANN001
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def get(self, url, headers=None, params=None):  # noqa: ANN001, ANN201
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["params"] = params or {}
            return _Resp()

    monkeypatch.setattr(config_routes.httpx, "AsyncClient", _Client)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/config/provider-models",
        json={
            "provider": {
                "api_type": "gemini",
                "api_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": "gk-test",
            }
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert captured["headers"]["Authorization"] == "Bearer gk-test"
    assert captured["params"] == {}
    assert [m["id"] for m in body["models"]] == ["gemini-2.5-flash", "gemini-2.5-pro"]


def test_config_provider_models_cli_routes_return_selectable_candidates(_runtime_context) -> None:  # noqa: ANN001
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    cases = {
        "gemini_cli": "gemini-2.5-flash",
        "antigravity_cli": "gemini-3.5-flash-low",
        "claude_code": "claude-opus-4-7",
        "openai_codex": "gpt-5.3-codex",
    }
    for api_type, expected in cases.items():
        res = client.post(
            "/personification/api/config/provider-models",
            json={"provider": {"api_type": api_type, "model": ""}},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["source"] == "local_cache"
        assert expected in {item["id"] for item in body["models"]}


def test_config_provider_models_endpoint_normalizes_version_paths() -> None:
    config_routes = load_personification_module("plugin.personification.webui.routes.config_routes")

    anthropic_url, anthropic_headers, _, anthropic_parser = config_routes._models_endpoint(  # noqa: SLF001
        {"api_type": "anthropic", "api_url": "https://api.anthropic.com/v1", "api_key": "ak-test"}
    )
    assert anthropic_url == "https://api.anthropic.com/v1/models"
    assert anthropic_headers["x-api-key"] == "ak-test"
    assert anthropic_headers["anthropic-version"]
    assert anthropic_parser == "anthropic"

    gemini_url, _, gemini_params, gemini_parser = config_routes._models_endpoint(  # noqa: SLF001
        {
            "api_type": "gemini",
            "api_url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-test:generateContent",
            "api_key": "gk-test",
        }
    )
    assert gemini_url == "https://generativelanguage.googleapis.com/v1beta/models"
    assert gemini_params["key"] == "gk-test"
    assert gemini_parser == "gemini"

    gemini_openai_url, gemini_openai_headers, gemini_openai_params, gemini_openai_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {
                "api_type": "gemini",
                "api_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                "api_key": "gk-openai",
            }
        )
    )
    assert gemini_openai_url == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert gemini_openai_headers["Authorization"] == "Bearer gk-openai"
    assert gemini_openai_params == {}
    assert gemini_openai_parser == "gemini_openai"

    openai_gemini_url, openai_gemini_headers, openai_gemini_params, openai_gemini_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {
                "api_type": "openai",
                "api_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "gk-openai",
            }
        )
    )
    assert openai_gemini_url == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert openai_gemini_headers["Authorization"] == "Bearer gk-openai"
    assert openai_gemini_params == {}
    assert openai_gemini_parser == "openai"

    zellon_openai_url, zellon_openai_headers, zellon_openai_params, zellon_openai_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {"api_type": "openai", "api_url": "https://anti.zellon.me", "api_key": "sk-zellon"}
        )
    )
    assert zellon_openai_url == "https://anti.zellon.me/v1/models"
    assert zellon_openai_headers["Authorization"] == "Bearer sk-zellon"
    assert zellon_openai_params == {}
    assert zellon_openai_parser == "openai"

    zellon_gemini_url, zellon_gemini_headers, zellon_gemini_params, zellon_gemini_parser = (  # noqa: SLF001
        config_routes._models_endpoint(
            {"api_type": "gemini", "api_url": "https://anti.zellon.me", "api_key": "sk-zellon"}
        )
    )
    assert zellon_gemini_url == "https://anti.zellon.me/v1beta/models"
    assert zellon_gemini_headers["Authorization"] == "Bearer sk-zellon"
    assert zellon_gemini_params == {}
    assert zellon_gemini_parser == "gemini"


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


def test_persona_template_builder_uses_main_model(_runtime_context, monkeypatch) -> None:
    persona_template_routes = load_personification_module("plugin.personification.webui.routes.persona_template_routes")

    async def _fake_sources(*, runtime, work_title, character_name):
        return [
            {
                "kind": "wiki",
                "query": f"{work_title} {character_name}",
                "source": "萌娘百科",
                "title": character_name,
                "url": "https://example.test/character",
                "summary": "角色资料摘要",
                "confidence": 0.9,
            }
        ]

    monkeypatch.setattr(persona_template_routes, "_gather_persona_sources", _fake_sources)

    class _MainCaller:
        def __init__(self):
            self.calls = []
            self.contexts = []

        async def __call__(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            llm_context = load_personification_module("plugin.personification.core.llm_context")
            self.contexts.append(dict(llm_context.current_llm_context()))
            user_text = str(messages[-1]["content"])
            if "生成插件内可直接使用的人设 YAML" in user_text:
                return """
name: 测试角色
tts:
  voice: default_zh
  style: 平静 自然
  user_hint: 用自然语气朗读。
status: |
  心情: "平静"
  状态: "测试中"
  记忆: ""
  动作: "看群消息"
nick_name:
  - 测试角色
ack_phrases:
  - 我看看
initial_message: "我是测试角色"
mute_keyword:
  - 闭嘴
input: |
  # 当前时间
  {time}
  # 触发原因
  {trigger_reason}
  {schedule_instruction}
  # 对话历史
  {history_new}
  # 当前消息
  {history_last}
  # 当前状态
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是测试角色，不是 AI 助手。
  ## 资料冲突与缺口
  - 无
""".strip()
            if "输出 JSON" in user_text:
                return '{"facts":["事实 S1"],"conflicts":[],"unknowns":[]}'
            return "ok"

    caller = _MainCaller()
    old_get_bots = _runtime_context.app_module.get_runtime_context().get_bots
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=old_get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(call_ai_api=caller),
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/persona-template/build",
        json={"work_title": "测试作品", "character_name": "测试角色"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["model_role"] == "configured_main"
    assert body["sources"][0]["source"] == "萌娘百科"
    assert len(body["subagents"]) == 3
    assert body["template_valid"] is True
    assert "system:" in body["template"]
    assert "input" in body["template_keys"]
    assert body["history_record"]["work_title"] == "测试作品"
    assert Path(body["export_path"]).is_file()
    history = client.get("/personification/api/persona-template/history?limit=5")
    assert history.status_code == 200, history.text
    assert history.json()["records"][0]["record_id"] == body["history_record"]["record_id"]
    detail = client.get(
        "/personification/api/persona-template/history/" + body["history_record"]["record_id"]
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["result"]["template"] == body["template"]
    apply_path = Path(_runtime_context.plugin_config.personification_data_dir) / "active_persona.yaml"
    _runtime_context.plugin_config.personification_prompt_path = str(apply_path)
    applied = client.post(
        "/personification/api/persona-template/apply",
        json={"record_id": body["history_record"]["record_id"]},
    )
    assert applied.status_code == 200, applied.text
    assert Path(applied.json()["path"]).is_file()
    assert "system:" in apply_path.read_text(encoding="utf-8")
    assert len(caller.calls) == 4
    assert all(call["kwargs"].get("use_builtin_search") is True for call in caller.calls[:3])
    purposes = [ctx.get("purpose") for ctx in caller.contexts]
    assert purposes == [
        "persona_template_research",
        "persona_template_research",
        "persona_template_research",
        "persona_template_synthesis",
    ]


def test_persona_template_builder_supports_custom_description(_runtime_context) -> None:
    class _MainCaller:
        def __init__(self):
            self.calls = []
            self.contexts = []

        async def __call__(self, messages, **kwargs):
            self.calls.append({"messages": messages, "kwargs": kwargs})
            llm_context = load_personification_module("plugin.personification.core.llm_context")
            self.contexts.append(dict(llm_context.current_llm_context()))
            user_text = str(messages[-1]["content"])
            if "原创人设描述" in user_text or "用户描述资料" in user_text:
                return """
name: 星野露
tts:
  voice: default_zh
  style: 平静 自然
  user_hint: 用自然语气朗读。
status: |
  心情: "平静"
  状态: "观察群聊"
  记忆: ""
  动作: "看群消息"
nick_name:
  - 星野露
ack_phrases:
  - 我看看
initial_message: "我是星野露"
mute_keyword:
  - 闭嘴
input: |
  # 当前时间
  {time}
  # 触发原因
  {trigger_reason}
  {schedule_instruction}
  # 对话历史
  {history_new}
  # 当前消息
  {history_last}
  # 当前状态
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是星野露，不是 AI 助手。
  ## 角色身份与不可替换锚点
  - 自定义人设。
  ## 资料冲突与缺口
  - 无
""".strip()
            return '{"facts":["用户描述事实"],"conflicts":[],"unknowns":[]}'

    caller = _MainCaller()
    old_get_bots = _runtime_context.app_module.get_runtime_context().get_bots
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=old_get_bots,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(call_ai_api=caller),
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    res = client.post(
        "/personification/api/persona-template/build",
        json={
            "mode": "custom",
            "persona_name": "星野露",
            "gender": "女",
            "personality": "温柔但会吐槽",
            "traits": "喜欢在群里用外号称呼熟人",
            "hobbies": "观星、游戏",
            "description": "一个夜猫子原创角色，说话轻，熟了之后会自然插话。",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "custom"
    assert body["work_title"] == "自定义人设"
    assert body["character_name"] == "星野露"
    assert body["sources"][0]["kind"] == "custom_description"
    assert body["template_valid"] is True
    assert len(body["subagents"]) == 3
    assert all(call["kwargs"].get("use_builtin_search") is False for call in caller.calls)
    purposes = [ctx.get("purpose") for ctx in caller.contexts]
    assert purposes == [
        "persona_template_custom_research",
        "persona_template_custom_research",
        "persona_template_custom_research",
        "persona_template_custom_synthesis",
    ]


def test_persona_template_builder_has_no_sample_specific_branches() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    source = module.Path(module.__file__).resolve()
    text = source.read_text(encoding="utf-8")
    forbidden_literals = [
        "绪山真寻",
        "西木野真姬",
        "早濑优香",
        "ONIMAI 官方",
        "LLWiki",
        "KivoWiki",
        "onimai.jp/character/mahiro",
    ]
    for literal in forbidden_literals:
        assert literal not in text


def test_persona_template_search_queries_use_generic_aliases() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    aliases = module._normalize_search_aliases(
        {
            "work_aliases": ["Example Work"],
            "character_aliases": ["Example Hero"],
            "queries": ["Example Work Example Hero character profile"],
        },
        work_title="测试作品",
        character_name="测试太郎",
    )
    queries = module._persona_search_queries("测试作品", "测试太郎", aliases)
    site_queries = module._persona_site_search_queries("测试作品", "测试太郎", aliases)

    assert "测试 太郎" in aliases["character_aliases"]
    assert any("Example Work" in query and "Example Hero" in query for query in queries)
    assert any("测试 太郎" in query for query in queries)
    assert any(query.startswith("site:") and "Example Hero" in query for query in site_queries)


def test_persona_template_source_relevance_rejects_weak_character_mentions() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    aliases = module._normalize_search_aliases(
        None,
        work_title="测试作品",
        character_name="测试太郎",
    )

    assert module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试太郎 - 萌娘百科",
        summary="测试太郎是《测试作品》的登场角色。",
        search_aliases=aliases,
    )
    assert module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试作品人物列表",
        summary="这里介绍测试作品角色，包括测试太郎。",
        search_aliases=aliases,
    )
    assert not module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试声优",
        summary="曾在测试作品中为测试太郎配音。",
        search_aliases=aliases,
    )
    assert not module._source_relevant(
        work_title="测试作品",
        character_name="测试太郎",
        title="测试太郎的母亲",
        summary="测试太郎的母亲是《测试作品》的登场角色。",
        search_aliases=aliases,
    )


def test_persona_template_validation_reports_quality_warnings() -> None:
    module = load_personification_module(
        "plugin.personification.webui.routes.persona_template_routes"
    )
    template = """
name: 测试角色
tts:
  voice: default_zh
status: |
  心情: "平静"
nick_name:
  - 测试角色
ack_phrases:
  - 我看看
initial_message: "我是测试角色"
mute_keyword:
  - 闭嘴
input: |
  {time}
  {trigger_reason}
  {schedule_instruction}
  {history_new}
  {history_last}
  {status}
  <output>
  <message>消息正文</message>
  </output>
system: |
  你是测试角色。作为助手回复用户问题。
  ## 资料冲突与缺口
  - 待确认
""".strip()

    validation = module._validate_template_yaml(template)

    assert validation["valid"] is True
    joined = "\n".join(validation["warnings"])
    assert "system 偏短" in joined
    assert "助手/客服式身份" in joined
    assert "ack_phrases" in joined
