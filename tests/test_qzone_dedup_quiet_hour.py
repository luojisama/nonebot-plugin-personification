"""QZone 主动发表的去重 + 半夜避开。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


diary_flow = load_personification_module("plugin.personification.flows.diary_flow")
periodic_jobs = load_personification_module("plugin.personification.jobs.periodic_jobs")


# ---- 字面去重收紧 ----


def test_dedup_catches_short_common_substring() -> None:
    """6 字公共子串就该判重（原来 8）。"""
    recent = ["今天又被卷死真是无语", "下班后吃了一碗拉面"]
    new = "卷死真是无语啊真累"
    # "卷死真是无语" 是 6 字公共子串
    assert diary_flow._is_too_similar_to_recent_qzone_post(new, recent) is True


def test_dedup_does_not_overmatch_unrelated() -> None:
    recent = ["今天吃了拉面"]
    # 主题完全不同，无显著公共子串
    new = "刚打完一局新作，节奏稀烂"
    assert diary_flow._is_too_similar_to_recent_qzone_post(new, recent) is False


def test_dedup_exact_substring_match_still_works() -> None:
    """完全包含关系仍判重。"""
    recent = ["突然觉得自由职业的代价就是没有同事吐槽"]
    new = "自由职业的代价就是没有同事吐槽"
    assert diary_flow._is_too_similar_to_recent_qzone_post(new, recent) is True


def test_dedup_skip_tiny_input() -> None:
    """太短的内容不参与判重。"""
    assert diary_flow._is_too_similar_to_recent_qzone_post("嗯嗯", ["以前发过别的"]) is False


# ---- 半夜 quiet hour ----


def test_in_quiet_hour_normal_window() -> None:
    """start=0, end=7：午夜到清晨 7 点全禁。"""
    now = datetime(2026, 5, 18, 3, 30)
    assert periodic_jobs._in_qzone_quiet_hour(now, 0, 7) is True


def test_in_quiet_hour_boundary_end_exclusive() -> None:
    """end 是开区间——7:00 整不算静默。"""
    now = datetime(2026, 5, 18, 7, 0)
    assert periodic_jobs._in_qzone_quiet_hour(now, 0, 7) is False


def test_in_quiet_hour_boundary_start_inclusive() -> None:
    """start 是闭区间——0:00 整就开始静默。"""
    now = datetime(2026, 5, 18, 0, 0)
    assert periodic_jobs._in_qzone_quiet_hour(now, 0, 7) is True


def test_in_quiet_hour_outside_window() -> None:
    now = datetime(2026, 5, 18, 14, 30)
    assert periodic_jobs._in_qzone_quiet_hour(now, 0, 7) is False


def test_in_quiet_hour_cross_midnight() -> None:
    """跨午夜窗口 22→7：晚上 23 点和凌晨 3 点都在内。"""
    assert periodic_jobs._in_qzone_quiet_hour(datetime(2026, 5, 18, 23, 0), 22, 7) is True
    assert periodic_jobs._in_qzone_quiet_hour(datetime(2026, 5, 18, 3, 0), 22, 7) is True
    assert periodic_jobs._in_qzone_quiet_hour(datetime(2026, 5, 18, 12, 0), 22, 7) is False


def test_in_quiet_hour_disabled_when_start_eq_end() -> None:
    """start == end 视为禁用静默期。"""
    assert periodic_jobs._in_qzone_quiet_hour(datetime(2026, 5, 18, 3, 0), 0, 0) is False
    assert periodic_jobs._in_qzone_quiet_hour(datetime(2026, 5, 18, 3, 0), 5, 5) is False


# ---- run_proactive_qzone_post 集成：quiet hour 直接 skip ----


def test_auto_diary_forces_cookie_refresh_before_generation() -> None:
    refresh_calls: list[bool] = []

    async def _update_cookie(_bot, *, force=False):
        refresh_calls.append(force)
        return True, "ok"

    async def _generate(_bot):
        return ""

    async def _publish(_content, _bot_id):
        raise AssertionError("empty generation must not publish")

    result = asyncio.run(periodic_jobs.run_auto_post_diary(
        qzone_publish_available=True,
        get_bots=lambda: {"1": SimpleNamespace(self_id="1")},
        update_qzone_cookie=_update_cookie,
        generate_ai_diary=_generate,
        publish_qzone_shuo=_publish,
        logger=SimpleNamespace(
            info=lambda *_a, **_k: None,
            warning=lambda *_a, **_k: None,
            error=lambda *_a, **_k: None,
        ),
    ))

    assert result is False
    assert refresh_calls == [True]


def test_proactive_qzone_post_skipped_in_quiet_hour() -> None:
    """3 点钟触发，quiet=0-7 → skip 且不调 publish_qzone_shuo。"""
    fake_now = datetime(2026, 5, 18, 3, 0)
    publish_called: list = []

    async def _fake_update_cookie(_bot, *, force=False):
        assert force is True
        return True, "ok"

    async def _fake_maybe_generate(_bot, quota=None):
        return "本来应该生成的内容"

    async def _fake_publish(content, self_id):
        publish_called.append((content, self_id))
        return True, "ok"

    result = asyncio.run(periodic_jobs.run_proactive_qzone_post(
        qzone_publish_available=True,
        qzone_proactive_enabled=True,
        qzone_probability=1.0,
        qzone_monthly_limit=10,
        qzone_min_interval_hours=0,
        get_bots=lambda: {"1": SimpleNamespace(self_id="1")},
        get_now=lambda: fake_now,
        update_qzone_cookie=_fake_update_cookie,
        maybe_generate_qzone_post=_fake_maybe_generate,
        publish_qzone_shuo=_fake_publish,
        logger=SimpleNamespace(debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
                               info=lambda *_a, **_k: None),
        quiet_hour_start=0,
        quiet_hour_end=7,
    ))
    assert result is False
    assert publish_called == [], "静默时段不应触发发布"


def test_proactive_qzone_post_runs_outside_quiet_hour(tmp_path, monkeypatch) -> None:
    """14 点正常时段：不被 quiet hour 拦截，会走到 cookie/generate 流程。"""
    data_store_mod = load_personification_module("plugin.personification.core.data_store")
    paths_mod = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store_mod.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))

    fake_now = datetime(2026, 5, 18, 14, 0)
    generated: list = []
    publish_called: list = []

    async def _fake_update_cookie(_bot, *, force=False):
        assert force is True
        return True, "ok"

    async def _fake_maybe_generate(_bot, quota=None):
        generated.append(True)
        return ""  # 返空避免真发布

    async def _fake_publish(content, self_id):
        publish_called.append((content, self_id))
        return True, "ok"

    result = asyncio.run(periodic_jobs.run_proactive_qzone_post(
        qzone_publish_available=True,
        qzone_proactive_enabled=True,
        qzone_probability=1.0,
        qzone_monthly_limit=10,
        qzone_min_interval_hours=0,
        get_bots=lambda: {"1": SimpleNamespace(self_id="1")},
        get_now=lambda: fake_now,
        update_qzone_cookie=_fake_update_cookie,
        maybe_generate_qzone_post=_fake_maybe_generate,
        publish_qzone_shuo=_fake_publish,
        logger=SimpleNamespace(debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
                               info=lambda *_a, **_k: None),
        quiet_hour_start=0,
        quiet_hour_end=7,
    ))
    # generate 应当被调用（说明 quiet hour 没拦下）
    assert generated == [True]
    # 但因为 generate 返空，最终未真正发布
    assert publish_called == []
    assert result is False


def test_proactive_qzone_probability_gate_skips_generation(tmp_path, monkeypatch) -> None:
    """概率闸门为 0 时，连 LLM 生成都不触发。"""
    _seed_state(tmp_path, monkeypatch, {"period": "2026-06", "count": 0, "last_post_at": 0, "recent_contents": []})
    fake_now = datetime(2026, 6, 18, 14, 0)
    generated: list = []
    published: list = []

    async def _fake_update_cookie(_bot, *, force=False):
        assert force is True
        raise AssertionError("probability gate should run before cookie refresh")

    async def _fake_maybe_generate(_bot, quota=None):
        generated.append(True)
        return "不该生成"

    async def _fake_publish(content, self_id):
        published.append((content, self_id))
        return True, "ok"

    result = asyncio.run(periodic_jobs.run_proactive_qzone_post(
        qzone_publish_available=True,
        qzone_proactive_enabled=True,
        qzone_probability=0.0,
        qzone_monthly_limit=30,
        qzone_min_interval_hours=0,
        get_bots=lambda: {"1": SimpleNamespace(self_id="1")},
        get_now=lambda: fake_now,
        update_qzone_cookie=_fake_update_cookie,
        maybe_generate_qzone_post=_fake_maybe_generate,
        publish_qzone_shuo=_fake_publish,
        logger=SimpleNamespace(debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
                               info=lambda *_a, **_k: None, error=lambda *_a, **_k: None),
        quiet_hour_start=0,
        quiet_hour_end=7,
    ))

    assert result is False
    assert generated == []
    assert published == []


# ---- 月度限额 ----


def _make_runner(now, *, monthly_limit, publish_called, generate=lambda: "今天写点什么"):
    async def _upd(_bot, *, force=False):
        assert force is True
        return True, "ok"

    async def _gen(_bot, quota=None):
        return generate()

    async def _pub(content, self_id):
        publish_called.append(content)
        return True, "ok"

    return periodic_jobs.run_proactive_qzone_post(
        qzone_publish_available=True,
        qzone_proactive_enabled=True,
        qzone_probability=1.0,
        qzone_monthly_limit=monthly_limit,
        qzone_min_interval_hours=0,
        get_bots=lambda: {"1": SimpleNamespace(self_id="1")},
        get_now=lambda: now,
        update_qzone_cookie=_upd,
        maybe_generate_qzone_post=_gen,
        publish_qzone_shuo=_pub,
        logger=SimpleNamespace(debug=lambda *_a, **_k: None, warning=lambda *_a, **_k: None,
                               info=lambda *_a, **_k: None, error=lambda *_a, **_k: None),
        quiet_hour_start=0,
        quiet_hour_end=7,
    )


def _seed_state(tmp_path, monkeypatch, state) -> None:
    data_store_mod = load_personification_module("plugin.personification.core.data_store")
    paths_mod = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths_mod, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store_mod.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    data_store_mod.get_data_store().save_sync("qzone_post_state", state)


def test_monthly_limit_blocks_after_quota(tmp_path, monkeypatch) -> None:
    """本月已发满 30 条 → 直接 skip，不发布。"""
    _seed_state(tmp_path, monkeypatch,
                {"period": "2026-06", "count": 30, "last_post_at": 0, "recent_contents": []})
    published: list = []
    result = asyncio.run(_make_runner(datetime(2026, 6, 17, 14, 0),
                                      monthly_limit=30, publish_called=published))
    assert result is False
    assert published == []


def test_monthly_limit_resets_on_new_month(tmp_path, monkeypatch) -> None:
    """跨月后计数归零，可继续发布，并写回新 period。"""
    _seed_state(tmp_path, monkeypatch,
                {"period": "2026-06", "count": 30, "last_post_at": 0, "recent_contents": []})
    published: list = []
    result = asyncio.run(_make_runner(datetime(2026, 7, 1, 14, 0),
                                      monthly_limit=30, publish_called=published))
    assert result is True
    assert len(published) == 1
    state = load_personification_module(
        "plugin.personification.core.data_store"
    ).get_data_store().load_sync("qzone_post_state")
    assert state["period"] == "2026-07"
    assert state["count"] == 1


def test_old_daily_state_migrates_to_monthly(tmp_path, monkeypatch) -> None:
    """旧的按天 state（只有 date 字段）被视为重置，按月重新计数。"""
    _seed_state(tmp_path, monkeypatch,
                {"date": "2026-08-01", "count": 3, "last_post_at": 0, "recent_contents": []})
    published: list = []
    result = asyncio.run(_make_runner(datetime(2026, 8, 15, 14, 0),
                                      monthly_limit=30, publish_called=published))
    assert result is True
    assert len(published) == 1
    state = load_personification_module(
        "plugin.personification.core.data_store"
    ).get_data_store().load_sync("qzone_post_state")
    assert state["period"] == "2026-08"
    assert state["count"] == 1


# ---- 月度额度快照 ----


def test_build_qzone_quota_current_month() -> None:
    q = periodic_jobs.build_qzone_quota(
        state={"period": "2026-06", "count": 25, "last_post_at": 0},
        now=datetime(2026, 6, 17, 14, 0),
        monthly_limit=30,
        min_interval_hours=6,
    )
    assert q["used"] == 25 and q["limit"] == 30 and q["remaining"] == 5
    assert q["days_in_month"] == 30 and q["days_left"] == 14


def test_build_qzone_quota_resets_for_new_month() -> None:
    """state 还停留在上个月时，本月已发计 0、额度全满。"""
    q = periodic_jobs.build_qzone_quota(
        state={"period": "2026-05", "count": 30, "last_post_at": 0},
        now=datetime(2026, 6, 1, 9, 0),
        monthly_limit=30,
        min_interval_hours=6,
    )
    assert q["used"] == 0 and q["remaining"] == 30


def test_build_qzone_quota_next_eligible_from_min_interval() -> None:
    last = datetime(2026, 6, 17, 12, 0).timestamp()
    q = periodic_jobs.build_qzone_quota(
        state={"period": "2026-06", "count": 1, "last_post_at": last},
        now=datetime(2026, 6, 17, 14, 0),
        monthly_limit=30,
        min_interval_hours=6,
    )
    assert q["next_eligible_at"] == last + 6 * 3600


def test_qzone_publish_reservation_allows_only_one_last_quota(tmp_path, monkeypatch) -> None:
    _seed_state(tmp_path, monkeypatch, {"period": "2026-06", "count": 0, "last_post_at": 0})
    now = datetime(2026, 6, 17, 14, 0)
    calls = []

    async def publish():
        calls.append(True)
        await asyncio.sleep(0.01)
        return True, "ok"

    async def run():
        return await asyncio.gather(*[
            periodic_jobs.coordinated_qzone_publish(
                operation_id=f"concurrent-{index}",
                content=f"内容 {index}",
                now=now,
                monthly_limit=1,
                min_interval_hours=0,
                kind="post",
                publish=publish,
            )
            for index in range(2)
        ])

    results = asyncio.run(run())
    assert sum(bool(item["success"]) for item in results) == 1
    assert len(calls) == 1
    state = load_personification_module("plugin.personification.core.data_store").get_data_store().load_sync("qzone_post_state")
    assert state["count"] == 1


def test_qzone_publish_operation_is_idempotent_and_timeout_is_unknown(tmp_path, monkeypatch) -> None:
    _seed_state(tmp_path, monkeypatch, {"period": "2026-06", "count": 0, "last_post_at": 0})
    now = datetime(2026, 6, 17, 14, 0)
    calls = []

    async def publish_ok():
        calls.append("ok")
        return True, "ok"

    first = asyncio.run(periodic_jobs.coordinated_qzone_publish(
        operation_id="same-operation",
        content="同一内容",
        now=now,
        monthly_limit=3,
        min_interval_hours=0,
        kind="post",
        publish=publish_ok,
    ))
    duplicate = asyncio.run(periodic_jobs.coordinated_qzone_publish(
        operation_id="same-operation",
        content="同一内容",
        now=now,
        monthly_limit=3,
        min_interval_hours=0,
        kind="post",
        publish=publish_ok,
    ))

    async def publish_timeout():
        raise asyncio.TimeoutError()

    unknown = asyncio.run(periodic_jobs.coordinated_qzone_publish(
        operation_id="unknown-operation",
        content="结果未知",
        now=now,
        monthly_limit=3,
        min_interval_hours=0,
        kind="forward",
        publish=publish_timeout,
    ))

    assert first["success"] is True
    assert duplicate["success"] is True and duplicate["duplicate"] is True
    assert calls == ["ok"]
    assert unknown["status"] == "unknown"
