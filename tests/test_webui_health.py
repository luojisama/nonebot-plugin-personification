from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module

from .test_webui_smoke import _build_client, _login_as_admin, _runtime_context  # noqa: F401

diagnostics = load_personification_module("plugin.personification.core.diagnostics")
ai_routes = load_personification_module("plugin.personification.core.ai_routes")
visual_capabilities = load_personification_module("plugin.personification.core.visual_capabilities")
health_routes = load_personification_module("plugin.personification.webui.routes.health_routes")
qzone_auth = load_personification_module("plugin.personification.core.qzone_auth")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")


_OPERATION_FIELDS = {
    "ok",
    "code",
    "phase",
    "title",
    "message",
    "details",
    "steps",
    "warnings",
    "suggestion",
    "retryable",
    "partial",
    "outcome_unknown",
    "operation_id",
    "trace_id",
}


def _assert_operation_diagnostic(body: dict, *, ok: bool) -> None:
    assert _OPERATION_FIELDS <= body.keys()
    assert body["ok"] is ok
    assert isinstance(body["details"], list)
    assert isinstance(body["steps"], list)


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


def test_health_mcp_check_counts_native_tools_and_explains_authorization(monkeypatch) -> None:
    mcp_compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    monkeypatch.setattr(mcp_compat, "_REGISTERED_MCP_TOOLS", {})

    class _Registry:
        def __init__(self, tools):  # noqa: ANN001
            self._tools = tools

        def all(self):  # noqa: ANN201
            return list(self._tools)

    builtin = SimpleNamespace(metadata={"source_kind": "mcp_builtin"})
    checks = diagnostics._skill_checks(SimpleNamespace(), SimpleNamespace(tool_registry=_Registry([builtin])))
    mcp = next(item for item in checks if item["key"] == "mcp")

    assert mcp["status"] == "ok"
    assert "原生 1" in mcp["detail"]

    checks = diagnostics._skill_checks(SimpleNamespace(), SimpleNamespace(tool_registry=_Registry([])))
    mcp = next(item for item in checks if item["key"] == "mcp")
    assert mcp["status"] == "info"
    assert "服务运行不等于工具已对 Agent 生效" in mcp["detail"]


def test_health_tts_http_error_is_connectivity_warning(monkeypatch) -> None:
    async def _not_found(_url, *, timeout=8):  # noqa: ARG001
        return True, "HTTP 404"

    monkeypatch.setattr(diagnostics, "_http_reachable", _not_found)
    cfg = SimpleNamespace(
        personification_tts_enabled=True,
        personification_tts_api_url="https://tts.example.test/v1",
        personification_tts_api_key="configured",
    )

    check = asyncio.run(diagnostics._tts_checks(cfg))[0]

    assert check["status"] == "warn"
    assert "尚未验证语音合成" in check["detail"]
    assert "/chat/completions" in check["hint"]


def test_health_only_runs_single_category(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    body = client.get("/personification/api/health/check", params={"only": "存储"}).json()
    assert body["partial"] is True
    assert [c["name"] for c in body["categories"]] == ["存储"]
    assert body["code"] == "health_category_rechecked"
    assert body["phase"] == "health_recheck"
    _assert_operation_diagnostic(body, ok=True)


def test_health_caches_full_run_and_serves_fast(_runtime_context, monkeypatch) -> None:
    diag = load_personification_module("plugin.personification.core.diagnostics")
    monkeypatch.setattr(diag, "_CACHE", {"result": None})
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    # refresh 真跑并写缓存
    first = client.get("/personification/api/health/check", params={"refresh": "true"}).json()
    assert first["cached"] is False
    assert first["code"] == "health_refresh_completed"
    assert first["phase"] == "health_refresh"
    _assert_operation_diagnostic(first, ok=True)
    # 默认读缓存
    second = client.get("/personification/api/health/check").json()
    assert second["cached"] is True
    assert second["generated_at"] == first["generated_at"]


def test_health_refresh_exception_is_structured_without_raw_message(_runtime_context, monkeypatch) -> None:
    async def _fail(**_kwargs):
        raise RuntimeError("raw-refresh-secret")

    monkeypatch.setattr(diagnostics, "run_diagnostics", _fail)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    response = client.get("/personification/api/health/check", params={"refresh": "true"})

    assert response.status_code == 500
    report = response.json()["detail"]
    assert report["code"] == "health_refresh_failed"
    assert report["phase"] == "health_refresh"
    assert report["trace_id"]
    assert "raw-refresh-secret" not in response.text
    _assert_operation_diagnostic(report, ok=False)


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


def test_video_probe_accepts_raw_uploaded_video_and_cleans_workspace(_runtime_context, monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    probe_root = tmp_path / "health-probes"
    _runtime_context.plugin_config.personification_health_probe_dir = str(probe_root)

    async def _fake_run_diagnostics(**kwargs):
        path = Path(kwargs["video_path"])
        captured["path"] = path
        assert path.is_file()
        assert path.read_bytes() == b"fake-mp4-content"
        return {
            "generated_at": 1,
            "overall": "ok",
            "summary": {"ok": 1, "warn": 0, "error": 0, "disabled": 0, "info": 0},
            "categories": [{
                "name": "视频理解",
                "checks": [{"key": "video_probe", "label": "视频理解真实探测", "status": "ok", "detail": "video_probe_status=ok", "hint": ""}],
            }],
        }

    monkeypatch.setattr(diagnostics, "run_diagnostics", _fake_run_diagnostics)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    response = client.post(
        "/personification/api/health/video-probe",
        content=b"fake-mp4-content",
        headers={
            "content-type": "video/mp4",
            "x-personification-video-filename": "uploaded.mp4",
        },
    )

    assert response.status_code == 200, response.text
    assert captured["path"]
    assert not Path(captured["path"]).exists()
    assert not probe_root.exists()
    assert "fake-mp4-content" not in response.text


def test_video_probe_rejects_unsupported_upload_without_model_call(_runtime_context, monkeypatch) -> None:
    called = False

    async def _fail(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("invalid upload must not call diagnostics")

    monkeypatch.setattr(diagnostics, "run_diagnostics", _fail)
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    response = client.post(
        "/personification/api/health/video-probe",
        content=b"not-video",
        headers={"content-type": "application/octet-stream", "x-personification-video-filename": "notes.txt"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "video_probe_invalid_type"
    assert not called


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


def _install_fake_interaction_runtime(
    _runtime_context,
    monkeypatch,
    *,
    group_id: str = "123456",
    user_id: str = "20001",
    rule_exc: Exception | None = None,
    authorize_test_user: bool = True,
):
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
        if rule_exc is not None:
            raise rule_exc
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
        superusers={"10001", *({user_id} if authorize_test_user else set())},
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
    assert body["ok"] is True
    assert body["code"] == "health_interaction_replied"
    _assert_operation_diagnostic(body, ok=True)
    assert body["diagnosis_code"] == "ok"
    assert "自检回复" in body["reply"]
    stage_keys = [item["key"] for item in body["stages"]]
    assert "rule_match" in stage_keys
    assert "buffer_dispatch" in stage_keys
    assert "fake_inner" in stage_keys
    assert "capture_reply" in stage_keys
    assert {"rule_match", "buffer_dispatch", "fake_inner", "capture_reply"} <= {
        item["key"] for item in body["steps"]
    }
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
    assert body["ok"] is True
    assert body["code"] == "health_interaction_replied"
    _assert_operation_diagnostic(body, ok=True)
    assert body["target"] == "group"
    assert body["diagnosis_code"] == "ok"
    assert "自检回复" in body["reply"]
    assert body["target_detail"]["group_id"] == _runtime_context.plugin_config.personification_webui_test_group_id


def test_interaction_test_rejects_non_admin_injected_identity_before_event_dispatch(
    _runtime_context,
    monkeypatch,
) -> None:
    sent = _install_fake_interaction_runtime(
        _runtime_context,
        monkeypatch,
        user_id="20001",
        authorize_test_user=False,
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)
    sent_before_test = len(sent)

    response = client.post(
        "/personification/api/health/interaction-test",
        json={"target": "group", "text": "不应注入"},
    )

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "health_interaction_test_identity_forbidden"
    assert detail["trace_id"]
    assert sent[sent_before_test:] == []
    audit = load_personification_module("plugin.personification.core.webui_audit_log")
    rows = audit.query_recent(action="health_interaction_test", limit=5)
    assert rows[0]["outcome"] == "forbidden"
    assert rows[0]["detail"]["target_qq"] == "20001"
    assert "不应注入" not in str(rows[0])


def test_interaction_exception_is_structured_without_raw_message(_runtime_context, monkeypatch) -> None:
    _install_fake_interaction_runtime(
        _runtime_context,
        monkeypatch,
        rule_exc=RuntimeError("raw-interaction-secret"),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post("/personification/api/health/interaction-test", json={"target": "group"})

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "health_interaction_rule_exception"
    assert body["diagnosis_code"] == "rule_exception"
    assert body["trace_id"]
    assert "RuntimeError" in str(body["steps"])
    assert "raw-interaction-secret" not in response.text
    _assert_operation_diagnostic(body, ok=False)


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

    async def _update_cookie(bot, *, force=False):  # noqa: ANN001
        assert force is True
        cookie_updates.append(getattr(bot, "self_id", ""))
        return True, "uin=o10000; p_skey=must-not-leak; skey=also-secret;"

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
    assert body["code"] == "qzone_forward_published"
    assert body["phase"] == "qzone_publish"
    _assert_operation_diagnostic(body, ok=True)
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
    assert "must-not-leak" not in str(body)
    assert "also-secret" not in str(body)
    rows = audit_mod.query_recent(action="qzone_forward_test", limit=5)
    assert rows and rows[0]["target"] == "20001"
    assert rows[0]["outcome"] == "ok"
    assert "must-not-leak" not in str(rows)
    assert "also-secret" not in str(rows)


def test_qzone_forward_fetch_exception_is_structured_and_safe(_runtime_context) -> None:
    cfg = _runtime_context.plugin_config
    cfg.personification_qzone_monthly_limit = 30
    cfg.personification_qzone_min_interval_hours = 0

    class _Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN001, ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    class _Service:
        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("raw-fetch-secret")

        async def forward_feed(self, **_kwargs):  # noqa: ANN003
            raise AssertionError("publish must not run")

    async def _refresh(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        raise RuntimeError("raw-auth-secret")

    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"10000": _Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(qzone_social_service=_Service(), update_qzone_cookie=_refresh),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post(
        "/personification/api/health/qzone-forward-test",
        json={"target_user_id": "20001", "operation_id": "fetch-safe-op"},
    )

    assert response.status_code == 500
    report = response.json()["detail"]
    assert report["code"] == "qzone_forward_fetch_exception"
    assert report["phase"] == "qzone_fetch"
    assert report["operation_id"] == "fetch-safe-op"
    assert [item["key"] for item in report["steps"]] == ["quota", "auth", "fetch"]
    assert "raw-fetch-secret" not in response.text
    assert "raw-auth-secret" not in response.text
    _assert_operation_diagnostic(report, ok=False)


def test_qzone_forward_quota_block_is_structured_and_skips_external_calls(_runtime_context) -> None:
    data_store_mod = load_personification_module("plugin.personification.core.data_store")
    time_ctx = load_personification_module("plugin.personification.core.time_ctx")
    cfg = _runtime_context.plugin_config
    cfg.personification_qzone_monthly_limit = 1
    cfg.personification_qzone_min_interval_hours = 0
    data_store_mod.get_data_store().save_sync(
        "qzone_post_state",
        {"period": time_ctx.get_configured_now().strftime("%Y-%m"), "count": 1, "last_post_at": 0},
    )
    external_calls: list[str] = []

    class _Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN001, ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    class _Service:
        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            external_calls.append("fetch")
            return True, "ok", []

        async def forward_feed(self, **_kwargs):  # noqa: ANN003
            external_calls.append("publish")
            return True, "ok"

    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"10000": _Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(qzone_social_service=_Service()),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post(
        "/personification/api/health/qzone-forward-test",
        json={"target_user_id": "20001", "operation_id": "quota-block-op"},
    )

    assert response.status_code == 409
    report = response.json()["detail"]
    assert report["code"] == "qzone_forward_quota_blocked"
    assert report["phase"] == "quota_check"
    assert report["stage"] == "quota"
    assert report["quota"]["remaining"] == 0
    assert report["steps"][0]["status"] == "error"
    assert external_calls == []
    _assert_operation_diagnostic(report, ok=False)


def test_qzone_forward_timeout_reports_outcome_unknown(_runtime_context) -> None:
    cfg = _runtime_context.plugin_config
    cfg.personification_qzone_monthly_limit = 30
    cfg.personification_qzone_min_interval_hours = 0
    feed = {"feed_id": "unknown-feed", "owner_uin": "20001", "content": "可能已转发"}

    class _Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN001, ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN003
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    class _Service:
        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            return True, "ok", [feed]

        async def forward_feed(self, **_kwargs):  # noqa: ANN003
            raise TimeoutError("raw-publish-secret")

    async def _refresh(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        return True, "cookie-secret-must-not-leak"

    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"10000": _Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(qzone_social_service=_Service(), update_qzone_cookie=_refresh),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post(
        "/personification/api/health/qzone-forward-test",
        json={"target_user_id": "20001", "operation_id": "unknown-forward-op"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "qzone_forward_outcome_unknown"
    assert body["phase"] == "qzone_publish"
    assert body["outcome_unknown"] is True
    assert body["retryable"] is False
    assert body["operation_id"] == "unknown-forward-op"
    assert body["stage"] == "forward"
    assert body["feed"]["feed_id"] == "unknown-feed"
    assert body["steps"][-1]["status"] == "unknown"
    assert "raw-publish-secret" not in response.text
    assert "cookie-secret-must-not-leak" not in response.text
    _assert_operation_diagnostic(body, ok=False)


def test_health_frontend_persists_and_renders_operation_diagnostics() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-health-qq.js").read_text(encoding="utf-8")

    assert 'renderAdminOperations("health","功能体检操作诊断")' in source
    assert source.count('rememberAdminOperation("health"') >= 8
    assert "renderOperationHistory(" in source
    assert "renderOperationDiagnostic(ir)" not in source
    assert "renderOperationDiagnostic(result)" not in source
    assert "qzoneForwardOperationId" in source
    assert 'state.qzoneForwardResult = { ok:false, error:e.message }' not in source
    assert '"交互测试失败：" + e.message' not in source


def test_health_requires_auth(_runtime_context) -> None:
    client = _build_client(_runtime_context)
    assert client.get("/personification/api/health/check").status_code == 401


def test_qzone_status_and_operations_are_observable_without_cookie_leak(_runtime_context) -> None:
    cfg = _runtime_context.plugin_config
    cfg.personification_qzone_enabled = True
    cfg.personification_qzone_social_enabled = True
    cfg.personification_qzone_inbound_enabled = True
    cfg.personification_qzone_cookie = "uin=o10000; p_skey=must-not-leak;"
    calls = []

    class Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    class Scheduler:
        def get_job(self, job_id):  # noqa: ANN001
            return SimpleNamespace(next_run_time=None) if job_id else None

    async def refresh(_bot, *, force=False):
        calls.append(("refresh", force))
        return True, "p_skey=must-not-leak"

    async def social(*, force=False):
        calls.append(("social", force))
        return {"ok": True, "status": "success", "feeds_seen": 1}

    async def inbound(*, force=False):
        calls.append(("inbound", force))
        return {"ok": True, "status": "success", "inbound_comments": 1}

    _runtime_context.app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"10000": Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(
            qzone_publish_available=True,
            update_qzone_cookie=refresh,
            qzone_social_scan=social,
            qzone_inbound_poll=inbound,
            scheduler=Scheduler(),
        ),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    status = client.get("/personification/api/qzone/status")
    refreshed = client.post("/personification/api/qzone/refresh-cookie")
    scanned = client.post("/personification/api/qzone/scan-now", json={"kind": "inbound"})

    assert status.status_code == 200, status.text
    assert status.json()["cookie_configured"] is True
    assert status.json()["social"]["job"]["registered"] is True
    assert refreshed.json()["status"] == "refreshed"
    assert refreshed.json()["message"] == "ok"
    assert refreshed.json()["diagnostic"]["code"] == "qzone_cookie_refreshed"
    assert scanned.json()["inbound_comments"] == 1
    assert scanned.json()["diagnostic"]["code"] == "qzone_inbound_scan_completed"
    assert calls == [("refresh", True), ("inbound", True)]
    assert "must-not-leak" not in status.text + refreshed.text + scanned.text


def test_qzone_post_now_returns_exact_generation_diagnostic(_runtime_context) -> None:
    class Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN003, ANN201
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    async def generate(_bot):  # noqa: ANN001
        return ""

    async def detailed(_bot):  # noqa: ANN001
        return {
            "content": "",
            "diagnostic": {
                "ok": False,
                "code": "semantic_not_grounded",
                "phase": "semantic_review",
                "title": "草稿缺少事件依据",
                "message": "草稿描述了没有素材支持的具体经历。",
                "retryable": True,
                "details": [{"label": "事件依据", "value": "未通过", "status": "error"}],
                "steps": [{"key": "semantic", "label": "语义审阅", "status": "error", "message": "没有素材依据", "details": []}],
            },
        }

    setattr(generate, "detailed", detailed)

    async def refresh(_bot, *, force=False):  # noqa: ANN001
        assert force is True
        return True, "ok"

    async def publish(_content, _bot_id):  # noqa: ANN001
        raise AssertionError("generation rejection must not publish")

    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=lambda: {"10000": Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(
            qzone_generate_post=generate,
            publish_qzone_shuo=publish,
            update_qzone_cookie=refresh,
        ),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post(
        "/personification/api/qzone/post-now",
        json={"bot_id": "10000", "operation_id": "diagnostic-op"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "semantic_not_grounded"
    assert body["phase"] == "semantic_review"
    assert body["operation_id"] == "diagnostic-op"
    assert body["details"][0]["label"] == "事件依据"
    assert [item["key"] for item in body["steps"]] == ["cookie_refresh", "semantic"]
    assert body["steps"][0]["status"] == "ok"
    assert body["steps"][1]["status"] == "error"


def test_qzone_login_routes_bind_owner_require_csrf_and_disable_qr_cache(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    class Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN003, ANN201
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN003, ANN201
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    calls: list[tuple[str, str]] = []

    async def start(*, bot_id, owner_key, install_cookie):  # noqa: ANN001
        calls.append((bot_id, owner_key))
        assert callable(install_cookie)
        return {
            "session_id": "session-one",
            "bot_id": bot_id,
            "status": "waiting_scan",
            "message": "请扫码",
            "terminal": False,
            "qr_ready": True,
        }

    def status(session_id, *, owner_key):  # noqa: ANN001
        assert owner_key
        return {"session_id": session_id, "bot_id": "10000", "status": "waiting_confirm", "terminal": False}

    def qrcode(session_id, *, owner_key):  # noqa: ANN001
        assert session_id == "session-one" and owner_key
        return b"private-qr-png"

    async def cancel(session_id, *, owner_key):  # noqa: ANN001
        assert session_id == "session-one" and owner_key
        return {"session_id": session_id, "bot_id": "10000", "status": "cancelled", "terminal": True}

    monkeypatch.setattr(qzone_auth.qzone_login_manager, "start", start)
    monkeypatch.setattr(qzone_auth.qzone_login_manager, "status", status)
    monkeypatch.setattr(qzone_auth.qzone_login_manager, "qrcode", qrcode)
    monkeypatch.setattr(qzone_auth.qzone_login_manager, "cancel", cancel)
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=lambda: {"10000": Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)

    client.headers.pop("X-Personification-CSRF", None)
    missing_csrf = client.post("/personification/api/qzone/auth/login/start", json={"bot_id": "10000"})
    assert missing_csrf.status_code == 403
    _set_csrf(client)
    started = client.post("/personification/api/qzone/auth/login/start", json={"bot_id": "10000"})
    polled = client.get("/personification/api/qzone/auth/login/session-one/status")
    image = client.get("/personification/api/qzone/auth/login/session-one/qrcode")
    cancelled = client.post("/personification/api/qzone/auth/login/session-one/cancel")

    assert started.status_code == 200
    assert started.json()["diagnostic"]["code"] == "qzone_login_started"
    assert polled.json()["status"] == "waiting_confirm"
    assert polled.json()["diagnostic"]["code"] == "qzone_login_status_loaded"
    assert image.content == b"private-qr-png"
    assert image.headers["content-type"].startswith("image/png")
    assert "no-store" in image.headers["cache-control"]
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["diagnostic"]["code"] == "qzone_login_cancelled"
    assert calls and calls[0][0] == "10000"

    stranger = _build_client(_runtime_context)
    assert stranger.get("/personification/api/qzone/auth/login/session-one/qrcode").status_code == 401


def test_qzone_manual_cookie_import_never_echoes_or_audits_secret(_runtime_context, monkeypatch) -> None:  # noqa: ANN001
    secret = "uin=o10000; p_skey=manual-super-secret;"
    received: list[str] = []

    class Bot:
        self_id = "10000"

        async def call_api(self, _name, **kwargs):  # noqa: ANN003, ANN201
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

        async def send_private_msg(self, **kwargs):  # noqa: ANN003, ANN201
            _runtime_context.sent.append(kwargs)
            return {"message_id": 1}

    async def install(**kwargs):  # noqa: ANN003, ANN201
        received.append(str(kwargs.get("cookie") or ""))
        return True, "ok"

    monkeypatch.setattr(qzone_service, "install_qzone_cookie", install)
    _runtime_context.app_module.set_runtime_context(
        plugin_config=_runtime_context.plugin_config,
        superusers={"10001"},
        get_bots=lambda: {"10000": Bot()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(),
    )
    client = _build_client(_runtime_context)
    _login_as_admin(client, _runtime_context)
    _set_csrf(client)

    response = client.post(
        "/personification/api/qzone/auth/cookie",
        json={"bot_id": "10000", "cookie": secret},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "installed"
    assert response.json()["message"] == "QZone Cookie 已验证并安装"
    assert response.json()["diagnostic"]["code"] == "qzone_cookie_installed"
    assert received == [secret]
    assert secret not in response.text

    audit_mod = load_personification_module("plugin.personification.core.webui_audit_log")
    rows = audit_mod.query_recent(action="qzone_cookie_import", limit=5)
    assert rows
    assert "manual-super-secret" not in str(rows[0])
