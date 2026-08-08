from __future__ import annotations

import builtins
import copy
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event, RLock
from types import SimpleNamespace

from ._loader import load_personification_module

favorability = load_personification_module("plugin.personification.core.favorability")


class _FakeStore:
    def __init__(self) -> None:
        self.payload: dict[str, dict] = {}
        self.write_count = 0
        self._lock = RLock()

    def load_sync(self, name: str):  # noqa: ANN001
        assert name == "favorability_profiles"
        with self._lock:
            return copy.deepcopy(self.payload)

    def save_sync(self, name: str, data):  # noqa: ANN001
        assert name == "favorability_profiles"
        with self._lock:
            self.payload = copy.deepcopy(data)
            self.write_count += 1

    def mutate_sync(self, name: str, mutator):  # noqa: ANN001
        assert name == "favorability_profiles"
        with self._lock:
            current = copy.deepcopy(self.payload)
            updated = mutator(current)
            self.payload = copy.deepcopy(current if updated is None else updated)
            self.write_count += 1
            return copy.deepcopy(self.payload)


def _config(**overrides):  # noqa: ANN001
    data = {
        "personification_favorability_enabled": True,
        "personification_favorability_default_score": 0.0,
        "personification_favorability_group_default_score": 35.0,
        "personification_favorability_levels": {"初见": 0, "普通": 35, "挚友": 90},
        "personification_favorability_event_deltas": favorability.DEFAULT_FAVORABILITY_EVENT_DELTAS.copy(),
        "personification_favorability_daily_positive_cap": 5.0,
        "personification_favorability_group_daily_positive_cap": 10.0,
        "personification_favorability_daily_negative_cap": 30.0,
        "personification_favorability_event_log_limit": 50,
        "personification_favorability_decay_enabled": False,
        "personification_favorability_decay_idle_days": 14,
        "personification_favorability_decay_delta": -0.2,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_favorability_service_migrates_external_data_and_writes_internal(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    external_updates: list[tuple[str, dict]] = []
    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda uid: {"favorability": 42.5, "custom_title": "熟人"} if uid == "10001" else {},
        update_user_data=lambda uid, **patch: external_updates.append((uid, patch)),
        load_data=lambda: {"10001": {"favorability": 42.5, "custom_title": "熟人"}},
        get_level_name=lambda value: f"external:{value}",
    )
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_enabled=True,
            personification_favorability_default_score=5.0,
            personification_favorability_group_default_score=95.0,
            personification_favorability_levels={"初见": 0, "普通": 35, "挚友": 90},
        ),
        external=external,
    )

    profile = service.get_user_data("10001")

    assert profile["favorability"] == 42.5
    assert profile["custom_title"] == "熟人"
    assert store.payload["10001"]["source"] == "external_sign_in"

    service.update_user_data("10001", favorability=93, daily_interesting_count=1.2)

    assert service.get_user_data("10001")["favorability"] == 93
    assert service.get_level_name(93) == "挚友"
    assert external_updates[-1][0] == "10001"
    assert external_updates[-1][1]["favorability"] == 93
    assert external_updates[-1][1]["daily_interesting_count"] == 1.2


def test_favorability_service_uses_internal_defaults(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_default_score=7.0,
            personification_favorability_group_default_score=88.0,
        )
    )

    assert service.get_user_data("20002")["favorability"] == 7.0
    assert service.get_user_data("group_123")["favorability"] == 88.0


def test_positive_events_update_legacy_counters_and_daily_caps(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_default_score=10.0,
            personification_favorability_group_default_score=90.0,
            personification_favorability_daily_positive_cap=0.05,
            personification_favorability_group_daily_positive_cap=0.10,
        )
    )
    now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

    first = service.apply_user_interesting_chat("10001", now=now, group_id="200")
    second = service.apply_user_interesting_chat("10001", now=now, group_id="200")
    profile = service.get_user_data("10001")

    assert first["delta"] == 0.05
    assert second["delta"] == 0.0
    assert second["status"] == "capped"
    assert profile["favorability"] == 10.05
    assert profile["daily_interesting_count"] == 0.05
    assert profile["daily_positive_count"] == 0.05
    assert profile["favorability_events"][-1]["type"] == "user_interesting_chat"

    group_first = service.apply_group_good_atmosphere("9988", now=now)
    group_second = service.apply_group_good_atmosphere("9988", now=now)
    group_profile = service.get_user_data("group_9988")

    assert group_first["delta"] == 0.1
    assert group_second["delta"] == 0.0
    assert group_profile["daily_fav_count"] == 0.1
    assert group_profile["daily_positive_count"] == 0.1


def test_reply_interaction_event_uses_shared_daily_positive_cap(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_default_score=20.0,
            personification_favorability_daily_positive_cap=0.05,
        )
    )
    now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

    first = service.apply_user_reply_interaction("10001", now=now, group_id="200", is_direct=True)
    second = service.apply_user_reply_interaction("10001", now=now, group_id="200", is_random_chat=True)
    profile = service.get_user_data("10001")

    assert first["delta"] == 0.03
    assert second["delta"] == 0.02
    assert second["status"] == "applied"
    assert profile["favorability"] == 20.05
    assert profile["daily_positive_count"] == 0.05
    assert profile["favorability_events"][-1]["type"] == "user_reply_interaction"
    assert profile["favorability_events"][-1]["metadata"]["is_random_chat"] is True


def test_negative_event_blacklist_clamps_by_daily_cap(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_default_score=50.0,
            personification_favorability_daily_negative_cap=30.0,
        )
    )
    now = datetime(2026, 6, 24, 8, 0, tzinfo=timezone.utc)

    result = service.apply_perm_blacklist("10001", set_blacklisted=True, actor="admin", now=now)
    duplicate = service.apply_perm_blacklist("10001", set_blacklisted=True, actor="admin", now=now)
    profile = service.get_user_data("10001")

    assert result["delta"] == -30.0
    assert duplicate["delta"] == 0.0
    assert profile["favorability"] == 20.0
    assert profile["is_perm_blacklisted"] is True
    assert profile["blacklist_count"] == 1
    assert profile["daily_negative_count"] == 30.0
    assert profile["favorability_events"][-2]["type"] == "user_perm_blacklist"


def test_manual_set_score_bypasses_daily_cap_and_logs_event(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_group_default_score=100.0,
            personification_favorability_daily_negative_cap=1.0,
            personification_favorability_event_log_limit=2,
        )
    )

    service.set_score("group_123", 60.0, actor="admin")
    service.set_score("group_123", 80.0, actor="admin")
    profile = service.get_user_data("group_123")

    assert profile["favorability"] == 80.0
    assert len(profile["favorability_events"]) == 2
    assert profile["favorability_events"][-1]["type"] == "manual_adjust"
    assert profile["favorability_events"][-1]["actor"] == "admin"


def test_decay_is_opt_in_and_skips_recent_profiles(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    old = datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc)
    now = old + timedelta(days=20)
    store.payload = {
        "10001": {
            "favorability": 10.0,
            "created_at": int(old.timestamp()),
            "updated_at": int(old.timestamp()),
        },
        "10002": {
            "favorability": 10.0,
            "created_at": int(now.timestamp()),
            "updated_at": int(now.timestamp()),
        },
        "group_9": {
            "favorability": 100.0,
            "source": "personification",
            "created_at": int(old.timestamp()),
            "updated_at": int(old.timestamp()),
        },
    }
    disabled = favorability.FavorabilityService(plugin_config=_config())

    assert disabled.run_decay_once(now=now)["enabled"] is False
    assert disabled.get_user_data("10001")["favorability"] == 10.0

    enabled = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_decay_enabled=True,
            personification_favorability_decay_idle_days=14,
            personification_favorability_decay_delta=-0.2,
        )
    )
    result = enabled.run_decay_once(now=now)

    assert result["checked"] == 2
    assert result["decayed"] == 1
    assert enabled.get_user_data("10001")["favorability"] == 9.8
    assert enabled.get_user_data("10002")["favorability"] == 10.0
    assert enabled.get_user_data("group_9")["favorability"] == 35.0
    assert enabled.get_user_data("10001")["favorability_events"][-1]["type"] == "daily_decay"


def test_group_100_baseline_migration_only_changes_proven_automatic_profiles(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    old_ts = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp())
    clamped = {"type": "group_good_atmosphere", "delta": 0, "status": "clamped", "timestamp": old_ts}
    store.payload = {
        "group_auto": {"favorability": 100, "source": "personification", "created_at": old_ts},
        "group_missing_source": {"favorability": 100, "favorability_events": [], "created_at": old_ts},
        "group_clamped": {
            "favorability": 100,
            "source": "personification",
            "favorability_events": [clamped],
            "created_at": old_ts,
        },
        "group_manual": {
            "favorability": 100,
            "source": "personification",
            "favorability_events": [{"type": "manual_adjust", "delta": 0, "status": "clamped"}],
        },
        "group_external": {"favorability": 100, "source": "external_sign_in"},
        "group_nonzero": {
            "favorability": 100,
            "source": "personification",
            "favorability_events": [{"type": "group_good_atmosphere", "delta": 0.2, "status": "applied"}],
        },
        "group_blacklisted": {
            "favorability": 100,
            "source": "personification",
            "is_perm_blacklisted": True,
        },
    }
    service = favorability.FavorabilityService(plugin_config=_config())

    profiles = service.load_data()

    for key in ("group_auto", "group_clamped"):
        assert profiles[key]["favorability"] == 35.0
        assert profiles[key]["favorability_events"][-1]["type"] == "baseline_migration"
        assert profiles[key]["daily_positive_count"] == 0.0
        assert profiles[key]["last_relationship_activity_at"] == old_ts
    for key in ("group_missing_source", "group_manual", "group_external", "group_nonzero", "group_blacklisted"):
        assert profiles[key]["favorability"] == 100.0


def test_schema_v2_source_missing_group_is_preserved_when_external_is_unavailable(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    store.payload = {
        "group_legacy_external": {
            "schema_version": 2,
            "favorability": 100,
            "nickname": "旧 external 群档案",
            "favorability_events": [],
        }
    }
    service = favorability.FavorabilityService(plugin_config=_config())

    profile = service.load_data()["group_legacy_external"]

    assert profile["favorability"] == 100.0
    assert not any(event["type"] == "baseline_migration" for event in profile["favorability_events"])


def test_group_baseline_migration_respects_configured_target_and_external_provenance(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    store.payload = {
        "group_local": {"favorability": 100, "source": "personification"},
        "group_external": {"favorability": 100},
    }
    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda _uid: {},
        update_user_data=lambda *_a, **_k: None,
        load_data=lambda: {"group_external": {"nickname": "外部群档案"}},
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(
        plugin_config=_config(personification_favorability_group_default_score=50),
        external=external,
    )

    profiles = service.load_data()

    assert profiles["group_local"]["favorability"] == 50.0
    assert profiles["group_external"]["favorability"] == 100.0
    assert profiles["group_external"]["source"] == "external_sign_in"


def test_new_legacy_profile_without_timestamps_does_not_decay_immediately(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    store.payload = {"10001": {"favorability": 10.0}}
    now = datetime(2026, 7, 17, 8, tzinfo=timezone.utc)
    monkeypatch.setattr(favorability.time, "time", lambda: now.timestamp())
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_decay_enabled=True,
            personification_favorability_decay_idle_days=14,
        )
    )

    assert service.run_decay_once(now=now)["decayed"] == 0
    profile = service.peek_user_data("10001")
    assert profile is not None
    assert profile["last_relationship_activity_at"] == int(now.timestamp())


def test_external_migration_preserves_present_false_and_zero_and_runs_once(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    store.payload = {
        "10001": {
            "favorability": 0,
            "is_perm_blacklisted": False,
            "blacklist_count": 0,
            "source": "personification",
        }
    }
    reads = 0

    def _external_data():
        nonlocal reads
        reads += 1
        return {
            "10001": {
                "favorability": 88,
                "is_perm_blacklisted": True,
                "blacklist_count": 9,
                "nickname": "外部昵称",
            }
        }

    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda _uid: {},
        update_user_data=lambda *_a, **_k: None,
        load_data=_external_data,
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    first = service.get_user_data("10001")
    second = service.get_user_data("10001")

    assert first["favorability"] == 0.0
    assert first["is_perm_blacklisted"] is False
    assert first["blacklist_count"] == 0
    assert first["nickname"] == "外部昵称"
    assert first["external_migration_version"] == 1
    assert first["external_migration_at"] > 0
    assert second == first
    assert reads == 1


def test_external_default_for_absent_record_cannot_backfill_new_group(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    legacy_get_calls = 0

    def _legacy_get(_uid: str) -> dict:
        nonlocal legacy_get_calls
        legacy_get_calls += 1
        return {"favorability": 100, "daily_fav_count": 0, "last_update": ""}

    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=_legacy_get,
        update_user_data=lambda *_a, **_k: None,
        load_data=lambda: {},
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    profile = service.get_user_data("group_new")

    assert profile["favorability"] == 35.0
    assert profile["source"] == "personification"
    assert profile["external_migration_version"] == 1
    assert legacy_get_calls == 0


def test_bulk_external_migration_has_persisted_once_marker(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    bulk_reads = 0

    def _load_external():
        nonlocal bulk_reads
        bulk_reads += 1
        return {"20002": {"favorability": 12, "is_perm_blacklisted": False}}

    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda _uid: {},
        update_user_data=lambda *_a, **_k: None,
        load_data=_load_external,
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    assert service.load_data()["20002"]["favorability"] == 12.0
    first_writes = store.write_count
    assert service.load_data()["20002"]["favorability"] == 12.0

    assert bulk_reads == 1
    assert store.write_count == first_writes
    assert store.payload["20002"]["external_migration_version"] == 1


def test_event_id_is_idempotent_beyond_event_log_limit(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_event_log_limit=1,
            personification_favorability_daily_positive_cap=100,
        )
    )

    first = service.apply_event("10001", "custom", delta=1, event_id="event-1")
    service.apply_event("10001", "custom", delta=1, event_id="event-2")
    before_duplicate = service.peek_user_data("10001")
    duplicate = service.apply_event("10001", "custom", delta=1, event_id="event-1")
    after_duplicate = service.peek_user_data("10001")

    assert first["delta"] == 1.0
    assert duplicate["status"] == "duplicate"
    assert duplicate["delta"] == 0.0
    assert after_duplicate == before_duplicate
    assert after_duplicate is not None
    assert after_duplicate["favorability"] == 2.0
    assert after_duplicate["favorability_events"][0]["event_id"] == "event-2"
    assert after_duplicate["recent_event_ids"] == ["event-1", "event-2"]


def test_decay_runs_daily_after_idle_threshold_without_moving_activity(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    activity = datetime(2026, 6, 1, 8, tzinfo=timezone.utc)
    day_one = activity + timedelta(days=14)
    day_two = day_one + timedelta(days=1)
    store.payload = {
        "10001": {
            "favorability": 1.0,
            "created_at": int(activity.timestamp()),
            "updated_at": int(activity.timestamp()),
            "last_relationship_activity_at": int(activity.timestamp()),
            "schema_version": 3,
        }
    }
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_decay_enabled=True,
            personification_favorability_decay_idle_days=14,
            personification_favorability_decay_delta=-0.2,
        )
    )

    assert service.run_decay_once(now=day_one)["decayed"] == 1
    assert service.run_decay_once(now=day_one)["decayed"] == 0
    assert service.run_decay_once(now=day_two)["decayed"] == 1
    profile = service.peek_user_data("10001")

    assert profile is not None
    assert profile["favorability"] == 0.6
    assert profile["last_relationship_activity_at"] == int(activity.timestamp())
    assert profile["last_favorability_decay_date"] == "2026-06-16"


def test_peek_and_snapshot_are_pure_local_reads(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    store.payload = {"10001": {"favorability": 7, "source": "personification"}}
    before = copy.deepcopy(store.payload)
    external_reads: list[str] = []
    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda uid: external_reads.append(uid) or {"favorability": 99},
        update_user_data=lambda *_a, **_k: None,
        load_data=lambda: {},
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    profile = service.peek_user_data("10001")
    snapshot = service.snapshot_profiles()

    assert profile is not None and profile["schema_version"] == 3
    assert snapshot["10001"]["favorability"] == 7.0
    assert service.peek_user_data("missing") is None
    assert store.payload == before
    assert store.write_count == 0
    assert external_reads == []


def test_non_finite_scores_deltas_caps_and_thresholds_fall_back(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_default_score=7,
            personification_favorability_daily_positive_cap=float("inf"),
        )
    )

    service.set_score("10001", float("nan"))
    service.update_user_data("10001", favorability=float("inf"))
    infinite_delta = service.apply_event("10001", "custom", delta=float("inf"))
    nan_cap = service.apply_event("10001", "custom", delta=1, daily_cap=float("nan"))
    profile = service.peek_user_data("10001")
    levels = favorability.normalize_favorability_levels({"nan": float("nan"), "inf": float("inf")})
    deltas = favorability.normalize_favorability_event_deltas({"custom": float("inf")})

    assert profile is not None
    assert profile["favorability"] == 7.0
    assert infinite_delta["delta"] == 0.0
    assert nan_cap["delta"] == 0.0
    assert all(math.isfinite(value) for value in levels.values())
    assert all(math.isfinite(value) for value in deltas.values())
    assert all(math.isfinite(event["delta"]) for event in profile["favorability_events"])


def test_configured_timezone_controls_event_day(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(personification_timezone="Asia/Shanghai")
    )
    utc_evening = datetime(2026, 7, 16, 18, 30, tzinfo=timezone.utc)

    service.apply_event("10001", "custom", delta=1, now=utc_evening)
    profile = service.peek_user_data("10001")

    assert service.current_date(utc_evening) == "2026-07-17"
    assert profile is not None
    assert profile["last_favorability_event_date"] == "2026-07-17"


def test_concurrent_events_are_atomic_for_same_user_different_users_and_cap(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=_config(
            personification_favorability_event_log_limit=500,
            personification_favorability_daily_positive_cap=0.05,
        )
    )
    now = datetime(2026, 7, 17, 8, tzinfo=timezone.utc)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: service.apply_event("same", "custom", delta=0.1, daily_cap=-1, event_id=f"s-{i}", now=now), range(100)))
        list(
            pool.map(
                lambda item: service.apply_event(item[0], "custom", delta=0.1, daily_cap=-1, event_id=item[1], now=now),
                [("left", f"l-{i}") for i in range(50)] + [("right", f"r-{i}") for i in range(50)],
            )
        )
        list(pool.map(lambda i: service.apply_event("capped", "custom", delta=0.03, event_id=f"c-{i}", now=now), range(20)))

    snapshot = service.snapshot_profiles()
    assert snapshot["same"]["favorability"] == 10.0
    assert snapshot["same"]["revision"] == 100
    assert snapshot["left"]["favorability"] == 5.0
    assert snapshot["right"]["favorability"] == 5.0
    assert snapshot["capped"]["favorability"] == 0.05
    assert snapshot["capped"]["daily_positive_count"] == 0.05


def test_concurrent_blacklist_only_counts_first_transition(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(plugin_config=_config(personification_favorability_default_score=50))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _i: service.apply_perm_blacklist("10001", set_blacklisted=True), range(20)))

    profile = service.peek_user_data("10001")
    assert profile is not None
    assert profile["favorability"] == 20.0
    assert profile["blacklist_count"] == 1


def test_concurrent_external_mirror_finishes_with_latest_local_snapshot(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    external_updates: list[float] = []
    first_mirror_started = Event()
    release_first = Event()
    call_count = 0

    def _update_external(_uid: str, **patch) -> None:  # noqa: ANN003
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_mirror_started.set()
            assert release_first.wait(2)
        external_updates.append(float(patch["favorability"]))

    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda _uid: {},
        update_user_data=_update_external,
        load_data=lambda: {},
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.set_score, "10001", 10)
        assert first_mirror_started.wait(2)
        second = pool.submit(service.set_score, "10001", 20)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            current = service.peek_user_data("10001")
            if current is not None and current["favorability"] == 20:
                break
            time.sleep(0.01)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert service.peek_user_data("10001")["favorability"] == 20.0
    assert external_updates[-1] == 20.0


def test_external_adapter_initialization_exception_degrades_safely(monkeypatch) -> None:  # noqa: ANN001
    original_import = builtins.__import__

    def _raising_import(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if name == "plugin.sign_in.utils":
            raise RuntimeError("broken sign-in initialization")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _raising_import)

    adapter = favorability.build_external_sign_in_adapter()

    assert adapter.available is False
    assert adapter.get_user_data("10001") == {}


def test_external_mirror_failure_does_not_break_committed_local_write(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore()
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)

    def _raise_on_update(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        raise RuntimeError("mirror failed")

    external = favorability.ExternalFavorabilityAdapter(
        available=True,
        get_user_data=lambda _uid: {},
        update_user_data=_raise_on_update,
        load_data=lambda: {},
        get_level_name=lambda value: str(value),
    )
    service = favorability.FavorabilityService(plugin_config=_config(), external=external)

    result = service.apply_event("10001", "custom", delta=1)

    assert result["delta"] == 1.0
    assert service.peek_user_data("10001")["favorability"] == 1.0
