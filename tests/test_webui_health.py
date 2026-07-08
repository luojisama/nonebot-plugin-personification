from __future__ import annotations

from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401

diagnostics = load_personification_module("plugin.personification.core.diagnostics")
ai_routes = load_personification_module("plugin.personification.core.ai_routes")
visual_capabilities = load_personification_module("plugin.personification.core.visual_capabilities")
health_routes = load_personification_module("plugin.personification.webui.routes.health_routes")


class _FakeResp:
    def __init__(self, content="红绿蓝黄", raw=None):
        self.content = content
        self.raw = raw
        self.finish_reason = "stop"


class _FakeCaller:
    def __init__(self, content="红绿蓝黄"):
        self._c = content

    async def chat_with_tools(self, messages, tools, use_builtin_search):
        return _FakeResp(self._c)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """屏蔽真实网络/模型调用：模型探测用假 caller，连通探测直接返回可达。"""
    monkeypatch.setattr(ai_routes, "build_single_provider_caller", lambda cfg, prov, **kw: _FakeCaller())
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda cfg, lg: [
        {"name": "main", "api_type": "openai", "model": "gpt-4o", "priority": 1}
    ])

    async def _reachable(url, *, timeout=8):
        return True, "HTTP 200"

    monkeypatch.setattr(diagnostics, "_http_reachable", _reachable)
    monkeypatch.setattr(diagnostics, "_CACHE", {"result": None})  # 每个测试清空体检缓存


def _set_csrf(client) -> None:
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def _statuses(body: dict) -> dict:
    out = {}
    for cat in body["categories"]:
        for c in cat["checks"]:
            out[c["key"]] = c
    return out


def test_health_does_live_model_call(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check").json()
    assert body["live"] is True
    st = _statuses(body)
    # 模型探测产生 per-provider 项，且为真实调用结果
    model_keys = [k for k in st if k.startswith("model_")]
    assert model_keys, "应有逐 provider 的模型调用探测项"
    assert st[model_keys[0]]["status"] == "ok"
    assert "调用正常" in st[model_keys[0]]["detail"]


def test_health_model_call_failure_is_error(_runtime_context, monkeypatch) -> None:
    class _Boom:
        async def chat_with_tools(self, messages, tools, use_builtin_search):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(ai_routes, "build_single_provider_caller", lambda cfg, prov, **kw: _Boom())
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check", params={"refresh": "true"}).json()
    st = _statuses(body)
    model_keys = [k for k in st if k.startswith("model_")]
    assert st[model_keys[0]]["status"] == "error"
    assert "调用失败" in st[model_keys[0]]["detail"]
    assert body["overall"] == "error"


def test_health_db_is_live_query(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    st = _statuses(client.get("/personification/api/health/check").json())
    assert st["db"]["status"] == "ok"
    assert "查询正常" in st["db"]["detail"]


def test_health_only_runs_single_category(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check", params={"only": "存储"}).json()
    assert body["partial"] is True
    assert [c["name"] for c in body["categories"]] == ["存储"]


def test_health_caches_full_run_and_serves_fast(_runtime_context, monkeypatch) -> None:
    diag = load_personification_module("plugin.personification.core.diagnostics")
    monkeypatch.setattr(diag, "_CACHE", {"result": None})
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    # refresh 真跑并写缓存
    first = client.get("/personification/api/health/check", params={"refresh": "true"}).json()
    assert first["cached"] is False
    # 默认读缓存
    second = client.get("/personification/api/health/check").json()
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]


def test_health_includes_llm_subconfig_category(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check", params={"refresh": "true"}).json()
    assert "LLM 子模型" in [c["name"] for c in body["categories"]]
    assert "视觉能力" in [c["name"] for c in body["categories"]]


def test_health_visual_probe_refreshes_stale_negative_cache(_runtime_context) -> None:
    visual_capabilities.set_visual_capability(
        visual_capabilities.VISUAL_ROUTE_REPLY_PLAIN,
        "openai",
        "gpt-4o",
        False,
        source="unit",
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check", params={"only": "视觉能力"}).json()
    st = _statuses(body)
    assert st["vision_reply_plain"]["status"] == "ok"
    assert visual_capabilities.provider_supports_vision(
        "openai",
        "gpt-4o",
        route_name=visual_capabilities.VISUAL_ROUTE_REPLY_PLAIN,
    ) is True


def test_interaction_test_requires_configured_target(_runtime_context) -> None:
    cfg = _runtime_context.plugin_config
    cfg.personification_webui_test_group_id = ""
    cfg.personification_webui_test_user_id = ""
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    res = client.post("/personification/api/health/interaction-test", json={"target": "group"})
    assert res.status_code == 400


def test_interaction_wait_follows_reply_timeout() -> None:
    cfg = SimpleNamespace(personification_response_timeout=180)

    assert health_routes._interaction_wait_seconds(cfg) >= 185


def _install_fake_interaction_runtime(_runtime_context, monkeypatch, *, group_id: str = "123456", user_id: str = "20001"):
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment

    processor = load_personification_module("plugin.personification.handlers.reply_pipeline.processor")
    logger = SimpleNamespace(
        debug=lambda *_a, **_k: None,
        info=lambda *_a, **_k: None,
        warning=lambda *_a, **_k: None,
        error=lambda *_a, **_k: None,
        exception=lambda *_a, **_k: None,
    )
    sent: list[dict] = []

    class _FakeBot:
        self_id = "100"

        async def call_api(self, name: str, **kwargs):  # noqa: ANN001
            if name == "get_friend_list":
                return [{"user_id": int(user_id)}]
            sent.append({"api": name, **kwargs})
            return {"message_id": len(sent)}

        async def send(self, event, message, **kwargs):  # noqa: ANN001
            sent.append(
                {
                    "api": "send",
                    "message_type": getattr(event, "message_type", ""),
                    "message": str(message),
                    **kwargs,
                }
            )
            return {"message_id": len(sent)}

        async def get_group_member_info(self, **_kwargs):  # noqa: ANN001
            return {"shut_up_timestamp": 0}

    async def _rule(event, state):  # noqa: ANN001
        state["is_random_chat"] = False
        state["message_target"] = "bot"
        return True

    async def _fake_process_response_logic(bot, event, state, deps):  # noqa: ANN001
        trace_mod = load_personification_module("plugin.personification.core.reply_turn_trace")

        assert state["message_target"] == "bot"
        trace_mod.record_stage(key="fake_inner", label="内部链路", status="info", detail="已进入回复处理")
        await bot.send(event, "自检回复")

    class _NeverPoke:
        pass

    monkeypatch.setattr(processor, "process_response_logic", _fake_process_response_logic)
    cfg = _runtime_context.plugin_config
    cfg.personification_webui_test_group_id = group_id
    cfg.personification_webui_test_user_id = user_id
    cfg.personification_whitelist = [group_id]
    cfg.personification_turn_trace_enabled = True
    cfg.personification_webui_log_capture_level = "INFO"
    bundle = SimpleNamespace(
        personification_rule=_rule,
        reply_processor_deps=SimpleNamespace(runtime=SimpleNamespace(logger=logger)),
        msg_buffer={},
        poke_event_cls=_NeverPoke,
        message_event_cls=MessageEvent,
        group_message_event_cls=GroupMessageEvent,
        message_cls=Message,
        message_segment_cls=MessageSegment,
        finished_exception_cls=None,
    )
    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"100": _FakeBot()},
        logger=logger,
        runtime_bundle=bundle,
    )
    _runtime_context.sent = sent
    return sent


def test_interaction_test_private_uses_plugin_path_and_captures_reply(_runtime_context, monkeypatch) -> None:
    sent = _install_fake_interaction_runtime(_runtime_context, monkeypatch)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    res = client.post("/personification/api/health/interaction-test", json={"target": "private", "text": "在吗"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["replied"] is True
    assert body["diagnosis_code"] == "ok"
    assert "自检回复" in body["reply"]
    stage_keys = [item["key"] for item in body["stages"]]
    assert "rule_match" in stage_keys
    assert "buffer_dispatch" in stage_keys
    assert "fake_inner" in stage_keys
    assert "capture_reply" in stage_keys
    assert any(item.get("api") == "send" for item in sent)
    logs = load_personification_module("plugin.personification.core.plugin_runtime_logs")
    rows = logs.query_recent(trace_id=body["trace_id"], limit=20)
    assert any(row["source"] == "webui.health" for row in rows)


def test_interaction_test_group_uses_plugin_path_and_captures_reply(_runtime_context, monkeypatch) -> None:
    _install_fake_interaction_runtime(_runtime_context, monkeypatch)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    res = client.post("/personification/api/health/interaction-test", json={"target": "group", "text": "群测试"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["replied"] is True
    assert body["target"] == "group"
    assert body["diagnosis_code"] == "ok"
    assert "自检回复" in body["reply"]
    assert body["target_detail"]["group_id"] == _runtime_context.plugin_config.personification_webui_test_group_id


def test_qzone_forward_test_forwards_first_feed_and_counts_quota(_runtime_context) -> None:
    data_store_mod = load_personification_module("plugin.personification.core.data_store")
    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    cfg = _runtime_context.plugin_config
    cfg.personification_qzone_enabled = True
    cfg.personification_qzone_monthly_limit = 30
    cfg.personification_qzone_min_interval_hours = 0

    feed = {
        "feed_id": "feed-first",
        "owner_uin": "20001",
        "nickname": "好友A",
        "content": "第一条空间文案",
        "created_at": 123456,
        "unikey": "http://user.qzone.qq.com/20001/mood/feed-first",
    }

    class _FakeBot:
        self_id = "10000"

        async def call_api(self, _name: str, **kwargs):  # noqa: ANN001
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN001
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    class _FakeQzoneService:
        def __init__(self) -> None:
            self.fetches = []
            self.forwarded = []

        async def fetch_user_feeds(self, **kwargs):  # noqa: ANN003
            self.fetches.append(kwargs)
            return True, "ok", [feed, {"feed_id": "second"}]

        async def forward_feed(self, **kwargs):  # noqa: ANN003
            self.forwarded.append(kwargs)
            return True, "ok"

    service = _FakeQzoneService()
    cookie_updates = []

    async def _update_cookie(bot):  # noqa: ANN001
        cookie_updates.append(getattr(bot, "self_id", ""))
        return True, "ok"

    logger = SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None)
    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"10000": _FakeBot()},
        logger=logger,
        runtime_bundle=SimpleNamespace(
            qzone_social_service=service,
            update_qzone_cookie=_update_cookie,
        ),
    )

    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    res = client.post(
        "/personification/api/health/qzone-forward-test",
        json={"target_user_id": "20001", "forward_text": "这句有点东西"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["target_user_id"] == "20001"
    assert body["feed"]["feed_id"] == "feed-first"
    assert service.fetches[0]["target_uin"] == "20001"
    assert service.fetches[0]["count"] == 1
    assert service.forwarded[0]["feed"] == feed
    assert service.forwarded[0]["content"] == "这句有点东西"
    assert cookie_updates == ["10000"]

    state = data_store_mod.get_data_store().load_sync("qzone_post_state")
    assert state["count"] == 1
    assert state["forward_count"] == 1
    assert body["quota"]["used"] == 1
    rows = audit_mod.query_recent(action="qzone_forward_test", limit=5)
    assert rows and rows[0]["target"] == "20001"
    assert rows[0]["outcome"] == "ok"


def test_health_requires_auth(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    assert client.get("/personification/api/health/check").status_code == 401
