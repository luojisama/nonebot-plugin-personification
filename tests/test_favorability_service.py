from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ._loader import load_personification_module

favorability = load_personification_module("plugin.personification.core.favorability")


class _FakeStore:
    def __init__(self) -> None:
        self.payload: dict[str, dict] = {}

    def load_sync(self, name: str):  # noqa: ANN001
        assert name == "favorability_profiles"
        return self.payload

    def save_sync(self, name: str, data):  # noqa: ANN001
        assert name == "favorability_profiles"
        self.payload = data


def _config(**overrides):  # noqa: ANN001
    data = {
        "personification_favorability_enabled": True,
        "personification_favorability_default_score": 0.0,
        "personification_favorability_group_default_score": 100.0,
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
    assert external_updates[-1] == ("10001", {"favorability": 93, "daily_interesting_count": 1.2})


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
    assert enabled.get_user_data("group_9")["favorability"] == 100.0
    assert enabled.get_user_data("10001")["favorability_events"][-1]["type"] == "daily_decay"
