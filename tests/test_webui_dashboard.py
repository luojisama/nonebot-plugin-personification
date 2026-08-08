from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_with_data(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_quota_anthropic_monthly_tokens=0,
        personification_quota_openai_monthly_tokens=1000,
        personification_quota_gemini_cli_monthly_tokens=0,
        personification_quota_codex_monthly_tokens=0,
    )
    data_store.init_data_store(cfg)

    ledger = load_personification_module("plugin.personification.core.token_ledger")
    ledger.record_llm_call(model="gpt-x", prompt_tokens=100, completion_tokens=50, group_id="1")
    ledger.record_llm_call(model="gpt-x", prompt_tokens=200, completion_tokens=80, group_id="2")
    ledger.record_llm_call(model="gpt-y", prompt_tokens=30, completion_tokens=10, group_id="1")
    ledger.record_llm_call(model="gpt-z", prompt_tokens=70, completion_tokens=30, purpose="persona_template_synthesis")
    onebot_cache = load_personification_module("plugin.personification.core.onebot_cache")
    onebot_cache._clear_caches_for_testing()

    # 准备一份全局画像和一份群内画像
    memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=SimpleNamespace(warning=lambda *_a, **_k: None))
    store.initialize()
    store.upsert_core_profile(user_id="u_alpha", profile_text="全局画像 Alpha")
    store.upsert_local_profile(group_id="g1", user_id="u_alpha", profile_text="g1 中是常驻成员")

    utils = load_personification_module("plugin.personification.utils")
    utils.init_utils_config(cfg)
    now_ts = time.time()
    utils.record_group_msg(
        "g1",
        "Alpha",
        "这条先留个上下文",
        user_id="u_alpha",
        message_id="m-alpha",
        time=now_ts - 20,
    )
    utils.record_group_msg(
        "g1",
        "Beta",
        "回复 Alpha 的话",
        user_id="u_beta",
        message_id="m-beta",
        reply_to_msg_id="m-alpha",
        reply_to_user_id="u_alpha",
        time=now_ts - 10,
    )

    profile_service_mod = load_personification_module("plugin.personification.core.profile_service")
    profile_service = profile_service_mod.ProfileService(memory_store=store)
    user_profile_meta = load_personification_module("plugin.personification.core.user_profile_meta")
    profile_service.upsert_user_profile_meta(
        user_id="u_alpha",
        meta=user_profile_meta.build_user_profile_meta(
            "u_alpha",
            stranger_info={
                "nickname": "Alpha资料",
                "sex": "unknown",
                "age": 18,
                "qid": "qid-alpha",
                "longNick": "这里是 Alpha 的签名",
            },
            source="test_fixture",
            now_ts=now_ts,
        ),
        source="test_fixture",
    )

    favorability_mod = load_personification_module("plugin.personification.core.favorability")
    favorability_service = favorability_mod.FavorabilityService(plugin_config=cfg)
    favorability_service.set_score("u_alpha", 66.0, actor="test")
    favorability_service.set_score("group_g1", 88.0, actor="test")

    class _InfoBot:
        def __init__(self) -> None:
            self.group_list_calls = 0
            self.group_info_calls = 0

        async def get_group_list(self):
            self.group_list_calls += 1
            return [
                {"group_id": 1, "group_name": "测试一群"},
                {"group_id": 2, "group_name": "测试二群"},
            ]

        async def get_group_info(self, group_id: int):
            self.group_info_calls += 1
            names = {1: "测试一群", 2: "测试二群"}
            return {"group_id": group_id, "group_name": names.get(int(group_id), "")}

        async def get_stranger_info(self, user_id: int):
            return {
                "user_id": user_id,
                "nickname": "Alpha资料" if str(user_id) == "0" else "资料昵称",
                "longNick": "协议签名",
                "qid": "qid-live",
            }

    info_bot = _InfoBot()

    runtime_bundle = SimpleNamespace(
        profile_service=profile_service,
        memory_store=store,
        favorability_service=favorability_service,
        get_bots=lambda: {"1": info_bot},
    )

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=runtime_bundle,
    )
    return SimpleNamespace(
        plugin_config=cfg,
        app_module=app_module,
        runtime_bundle=runtime_bundle,
        store=store,
        ledger=ledger,
        info_bot=info_bot,
    )


def _build_client(runtime_context):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(runtime_context.app_module.build_router())
    return TestClient(app)


def _login(client) -> None:
    sent: list = []

    class _Bot:
        async def call_api(self, _name: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    runtime = client.app  # not used
    # 重新绑定 get_bots 让 verify code 能送达
    app_module = load_personification_module("plugin.personification.webui.app")
    ctx = app_module.get_runtime_context()
    ctx.get_bots = lambda: {"1": _Bot()}
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    res2 = client.post(
        "/personification/api/auth/verify",
        json={"qq": "10001", "code": code, "device_label": "测试"},
    )
    assert res2.status_code == 200, res2.text
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_dashboard_metrics_returns_token_summary(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/metrics/summary?window=30d")
    assert res.status_code == 200
    body = res.json()
    assert body["window"] == "month"
    assert body["total"]["total_tokens"] == 100 + 50 + 200 + 80 + 30 + 10 + 70 + 30
    assert body["total"]["call_count"] == 4
    assert len(body["series"]) == 30
    models = {row["model"]: row for row in body["by_model"]}
    assert models["gpt-x"]["total_tokens"] == 430
    assert models["gpt-y"]["total_tokens"] == 40
    assert models["gpt-z"]["total_tokens"] == 100
    distribution = {row["model"]: row for row in body["model_distribution"]}
    assert distribution["gpt-x"]["relative_width"] == 1.0
    groups = {row["group_id"]: row for row in body["by_group"]}
    assert groups["1"]["total_tokens"] == 100 + 50 + 30 + 10
    assert groups["2"]["total_tokens"] == 280
    assert sum(row["total_tokens"] for row in groups.values()) < body["total"]["total_tokens"]
    assert groups["1"]["group_name"] == "测试一群"
    assert groups["1"]["group_label"] == "测试一群"
    assert all("未命名群" not in row["group_label"] for row in groups.values())
    assert _runtime_with_data.info_bot.group_list_calls == 1
    assert _runtime_with_data.info_bot.group_info_calls == 0
    providers = {row["provider"]: row for row in body["provider_usage"]}
    assert providers["openai"]["total_tokens"] == body["total"]["total_tokens"]
    assert providers["openai"]["monthly_limit"] == 1000
    assert body["billing"]["cost_configured"] is False
    assert body["billing"]["quota"]["limit_tokens"] == 1000
    assert body["total_consumption"]["total"]["total_tokens"] == body["total"]["total_tokens"]
    assert body["total_consumption"]["total"]["call_count"] == body["total"]["call_count"]
    assert body["total_consumption"]["first_day"]
    assert body["total_consumption"]["last_day"]
    assert body["total_consumption"]["series"][-1]["cumulative_total_tokens"] == body["total"]["total_tokens"]
    assert {chart["key"] for chart in body["dashboard_overview"]["charts"]} == {"day", "week", "month", "total"}
    assert len(body["dashboard_overview"]["charts"]) == 4
    day_chart = next(chart for chart in body["dashboard_overview"]["charts"] if chart["key"] == "day")
    assert len(day_chart["series"]) == 24
    assert all("bucket_hour" in point for point in day_chart["series"])
    assert day_chart["series"][-1]["total_tokens"] == body["total"]["total_tokens"]
    purposes = {row["purpose"]: row for row in body["by_purpose"]}
    assert purposes["persona_template_synthesis"]["purpose_label"] == "人设构建：模板生成"
    assert purposes["persona_template_synthesis"]["total_tokens"] == 100
    overview_models = {row["model"]: row for row in body["dashboard_overview"]["model_usage"]}
    assert overview_models["gpt-x"]["call_count"] == 2
    assert overview_models["gpt-x"]["total_tokens"] == 430
    overview_groups = {row["group_id"]: row for row in body["dashboard_overview"]["group_usage"]}
    assert overview_groups["1"]["group_label"] == "测试一群"
    assert overview_groups["1"]["percent"] > 0
    overview_purposes = {row["purpose"]: row for row in body["dashboard_overview"]["purpose_usage"]}
    assert overview_purposes["persona_template_synthesis"]["purpose_label"] == "人设构建：模板生成"
    assert overview_purposes["persona_template_synthesis"]["percent"] > 0


def test_dashboard_query_bundle_deduplicates_caches_and_invalidates(monkeypatch, tmp_path) -> None:
    metrics_routes = load_personification_module("plugin.personification.webui.routes.metrics_routes")
    metrics_routes._reset_dashboard_cache_for_testing()
    generation = {"value": 7}
    calls = {"summary": [], "provider": 0, "total": 0}

    monkeypatch.setattr(metrics_routes, "get_db_path", lambda: tmp_path / "personification.db")
    monkeypatch.setattr(
        metrics_routes.token_ledger,
        "ledger_generation",
        lambda: generation["value"],
    )

    def _summary(window: str) -> dict:
        calls["summary"].append(window)
        return {"window": window, "total": {"total_tokens": 1}, "series": []}

    def _provider(window: str) -> dict:
        calls["provider"] += 1
        return {"window": window, "providers": []}

    def _total() -> dict:
        calls["total"] += 1
        return {"total": {"total_tokens": 1}}

    monkeypatch.setattr(metrics_routes.token_ledger, "query_summary", _summary)
    monkeypatch.setattr(metrics_routes.token_ledger, "query_provider_summary", _provider)
    monkeypatch.setattr(metrics_routes.token_ledger, "query_total_consumption", _total)

    first = metrics_routes._query_dashboard_bundle("month")
    first["selected"]["mutated"] = True
    second = metrics_routes._query_dashboard_bundle("month")
    assert calls == {"summary": ["day", "week", "month"], "provider": 1, "total": 1}
    assert "mutated" not in second["selected"]

    generation["value"] += 1
    metrics_routes._query_dashboard_bundle("month")
    assert calls == {
        "summary": ["day", "week", "month", "day", "week", "month"],
        "provider": 2,
        "total": 2,
    }


def test_dashboard_group_rows_never_emit_fake_unnamed_label() -> None:
    metrics_routes = load_personification_module("plugin.personification.webui.routes.metrics_routes")
    runtime = SimpleNamespace(runtime_bundle=SimpleNamespace(get_bots=lambda: {"1": SimpleNamespace()}))
    rows = [{"group_id": "999001", "total_tokens": 1, "call_count": 1}]
    annotated = asyncio.run(metrics_routes._annotate_group_rows(runtime, rows))
    assert annotated[0]["group_name"] == ""
    assert annotated[0]["group_label"] == "群 999001"
    assert annotated[0]["group_name_missing"] is True
    assert annotated[0]["group_lookup_status"] == "missing"
    assert "未命名群" not in annotated[0]["group_label"]


def test_dashboard_window_validation(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/metrics/summary?window=year")
    assert res.status_code == 422


def test_dashboard_frontend_charts_have_detail_affordances() -> None:
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "webui" / "static" / "app-dashboard.js").read_text(encoding="utf-8")
    style_css = (root / "webui" / "static" / "style.css").read_text(encoding="utf-8")

    assert "function dashboardTooltipAttr" in app_js
    assert "initDashboardTooltipEvents();" in app_js
    assert "class=\"dashboard-line-hotspot\"" in app_js
    assert "class=\"dashboard-pie-slice\"" in app_js
    assert "class=\"dashboard-pie-legend-row\"" in app_js
    assert app_js.count("data-dashboard-tooltip") >= 4
    assert app_js.count("tabindex=\"0\"") >= 3
    assert "openDashboardLineDetail" in app_js
    assert "openDashboardPieDetail" in app_js
    assert "renderDashboardDetailModal" in app_js
    assert "dashboardSeriesPointTitle" in app_js
    assert "提示词令牌" in app_js
    assert "回复令牌" in app_js
    assert ".dashboard-tooltip" in style_css
    assert ".dashboard-tooltip.visible" in style_css


def test_personas_list_and_detail(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/personas")
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is True
    listed = next(p for p in body["profiles"] if p["user_id"] == "u_alpha")
    assert listed["favorability"]["available"] is True
    assert listed["favorability"]["score"] == 66.0
    assert listed["avatar_url"].endswith("dst_uin=u_alpha&spec=640")
    assert listed["homepage_url"] == "https://user.qzone.qq.com/u_alpha"
    assert listed["qq_profile"]["signature"] == "这里是 Alpha 的签名"

    res2 = client.get("/personification/api/personas/u_alpha")
    assert res2.status_code == 200
    detail = res2.json()
    assert detail["core_profile"]["profile_text"] == "全局画像 Alpha"
    assert detail["core_profile"]["qq_profile"]["avatar_url"].endswith("dst_uin=u_alpha&spec=640")
    assert detail["core_profile"]["qq_profile"]["homepage_url"] == "https://user.qzone.qq.com/u_alpha"
    assert detail["core_profile"]["qq_profile"]["signature"] == "这里是 Alpha 的签名"
    assert detail["favorability"]["score"] == 66.0
    assert detail["favorability"]["events"][0]["label"] == "管理员手动调整"
    assert len(detail["local_profiles"]) == 1
    assert detail["local_profiles"][0]["group_id"] == "g1"


def test_groups_list_and_detail(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    res = client.get("/personification/api/groups")
    assert res.status_code == 200
    body = res.json()
    assert any(group["group_id"] == "g1" for group in body["groups"])

    alias_res = client.put(
        "/personification/api/groups/g1/aliases/u_alpha",
        json={"aliases": "阿尔法、alpha哥", "note": "常用外号"},
    )
    assert alias_res.status_code == 200, alias_res.text
    assert alias_res.json()["entry"]["aliases"] == ["阿尔法", "alpha哥"]
    alias_only_res = client.put(
        "/personification/api/groups/g1/aliases/u_beta",
        json={"aliases": ["小贝"]},
    )
    assert alias_only_res.status_code == 200, alias_only_res.text

    res2 = client.get("/personification/api/groups/g1/personas")
    assert res2.status_code == 200
    detail = res2.json()
    assert any(p["user_id"] == "u_alpha" for p in detail["profiles"])
    assert any(p["user_id"] == "u_beta" for p in detail["profiles"])
    assert detail["group_favorability"]["score"] == 88.0
    member = next(p for p in detail["profiles"] if p["user_id"] == "u_alpha")
    assert member["favorability"]["score"] == 66.0
    assert member["aliases"] == ["阿尔法", "alpha哥"]
    assert member["alias_note"] == "常用外号"
    assert "Alpha" in member["known_names"]
    assert "阿尔法" in member["known_names"]
    assert any(edge["direction"] == "in" and edge["peer_label"] == "Beta" for edge in member["relationship_edges"])
    beta = next(p for p in detail["profiles"] if p["user_id"] == "u_beta")
    assert beta["aliases"] == ["小贝"]

    aliases = client.get("/personification/api/groups/g1/aliases")
    assert aliases.status_code == 200
    assert {entry["user_id"] for entry in aliases.json()["aliases"]} >= {"u_alpha", "u_beta"}

    alias_mod = load_personification_module("plugin.personification.core.group_member_aliases")
    block = alias_mod.render_group_alias_context("g1", user_id="u_alpha", known_names={"u_alpha": ["Alpha"]})
    assert "群成员称呼映射" in block
    assert "可以优先使用对应群内称呼" in block
    assert "当前说话人：QQ u_alpha" in block
    assert "阿尔法 / alpha哥" in block

    res3 = client.get("/personification/api/groups/g1/style")
    assert res3.status_code == 200
    style = res3.json()
    assert style["group_id"] == "g1"
    assert style["style_text"] == ""  # 暂未写入

    schedule = client.get("/personification/api/groups/g1/schedule")
    assert schedule.status_code == 200
    assert schedule.json()["enabled"] is False
    saved_schedule = client.put(
        "/personification/api/groups/g1/schedule",
        json={"enabled": True, "schedule_prompt": "- 晚上更活跃\n- 作息仅作背景，不限制是否回复"},
    )
    assert saved_schedule.status_code == 200
    assert saved_schedule.json()["enabled"] is True
    assert "晚上更活跃" in saved_schedule.json()["schedule_prompt"]


def test_group_switch_writes_authoritative_group_config(_runtime_with_data) -> None:
    client = _build_client(_runtime_with_data)
    _login(client)
    utils = load_personification_module("plugin.personification.utils")
    _runtime_with_data.plugin_config.personification_whitelist = ["static-group"]
    utils.save_whitelist(["dynamic-group"])

    disabled = client.delete("/personification/api/groups/static-group/whitelist")
    assert disabled.status_code == 200
    assert utils.get_group_config("static-group")["enabled"] is False
    assert utils.is_group_whitelisted("static-group", ["static-group"]) is False

    enabled = client.post("/personification/api/groups/dynamic-group/whitelist")
    assert enabled.status_code == 200
    assert utils.get_group_config("dynamic-group")["enabled"] is True
    utils.save_whitelist([])
    assert utils.is_group_whitelisted("dynamic-group", []) is True

    listed = client.get("/personification/api/groups/whitelist")
    static = next(item for item in listed.json()["groups"] if item["group_id"] == "static-group")
    assert static["enabled"] is False
    assert static["source"] == "group_config"
    assert static["static_config_readonly"] is True


def test_profile_service_prompt_block(_runtime_with_data) -> None:
    svc = _runtime_with_data.runtime_bundle.profile_service
    current = svc.get_core_profile("u_alpha")
    profile_json = dict(current.profile_json if current else {})
    profile_json["structured"] = {
        "portrait": "Alpha 是群里的常驻成员，熟人感强，喜欢用短句接话。",
        "nickname_pref": "可以叫阿尔法",
        "interaction_advice": "像熟人一样自然插话，少写解释。",
    }
    svc.upsert_core_profile(user_id="u_alpha", profile_text="全局画像 Alpha", profile_json=profile_json)
    block = svc.build_prompt_block(user_id="u_alpha", group_id="g1")
    assert "## 用户档案" in block
    assert "[头像URL]" not in block
    assert "[主页] https://user.qzone.qq.com/u_alpha" in block
    assert "[个性签名] 这里是 Alpha 的签名" in block
    assert "人物轮廓: Alpha 是群里的常驻成员" in block
    assert "称呼偏好: 可以叫阿尔法" in block
    assert "互动建议: 像熟人一样自然插话" in block
    assert "全局画像 Alpha" not in block
    assert "g1 中是常驻成员" in block

    block2 = svc.build_prompt_block(user_id="u_missing")
    assert block2 == ""


def test_group_relation_graph_frontend_present() -> None:
    app_admin_js = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-groups.js").read_text(
        encoding="utf-8"
    )
    assert "renderGroupRelationGraph" in app_admin_js
    assert "群员关系图" in app_admin_js
    assert "relation-node" in app_admin_js
