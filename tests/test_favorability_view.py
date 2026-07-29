from __future__ import annotations

import copy
from threading import RLock
from types import SimpleNamespace

from ._loader import load_personification_module


favorability = load_personification_module("plugin.personification.core.favorability")
favorability_view = load_personification_module("plugin.personification.webui.routes.favorability_view")


class _FakeStore:
    def __init__(self, payload: dict) -> None:
        self.payload = copy.deepcopy(payload)
        self.write_count = 0
        self._lock = RLock()

    def load_sync(self, name: str):  # noqa: ANN001
        assert name == "favorability_profiles"
        with self._lock:
            return copy.deepcopy(self.payload)

    def mutate_sync(self, name: str, mutator):  # noqa: ANN001
        assert name == "favorability_profiles"
        with self._lock:
            updated = mutator(copy.deepcopy(self.payload))
            self.payload = copy.deepcopy(updated)
            self.write_count += 1
            return copy.deepcopy(updated)


def _runtime(service):  # noqa: ANN001
    return SimpleNamespace(runtime_bundle=SimpleNamespace(favorability_service=service))


def test_serialize_favorability_is_pure_and_zeros_stale_daily_counts(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore(
        {
            "10001": {
                "favorability": 12,
                "daily_positive_count": 2,
                "daily_positive_date": "2026-07-16",
                "daily_negative_count": 3,
                "daily_negative_date": "2026-07-16",
                "daily_fav_count": 4,
                "last_update": "2026-07-16",
                "daily_interesting_count": 5,
                "last_interesting_date": "2026-07-16",
            }
        }
    )
    before = copy.deepcopy(store.payload)
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=SimpleNamespace(
            personification_favorability_enabled=False,
            personification_favorability_default_score=0,
            personification_favorability_group_default_score=35,
        )
    )
    service.current_date = lambda now=None: "2026-07-17"

    view = favorability_view.serialize_favorability(_runtime(service), "10001", scope="user")

    assert view["available"] is True
    assert view["enabled"] is False
    assert view["exists"] is True
    assert view["daily_positive_count"] == 0.0
    assert view["daily_negative_count"] == 0.0
    assert view["daily_fav_count"] == 0.0
    assert view["daily_interesting_count"] == 0.0
    assert store.payload == before
    assert store.write_count == 0


def test_serialize_missing_profile_uses_virtual_default_without_creation(monkeypatch) -> None:  # noqa: ANN001
    store = _FakeStore({})
    monkeypatch.setattr(favorability, "get_data_store", lambda: store)
    service = favorability.FavorabilityService(
        plugin_config=SimpleNamespace(
            personification_favorability_enabled=True,
            personification_favorability_default_score=7,
            personification_favorability_group_default_score=35,
        )
    )

    view = favorability_view.serialize_favorability(_runtime(service), "group_123", scope="group")

    assert view["available"] is True
    assert view["enabled"] is True
    assert view["exists"] is False
    assert view["score"] == 35.0
    assert view["source"] == "virtual_default"
    assert store.payload == {}
    assert store.write_count == 0
