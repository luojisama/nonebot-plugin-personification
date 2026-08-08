from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def profile_store(tmp_path: Path):
    memory_store = load_personification_module("plugin.personification.core.memory_store")
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=False,
    )
    store = memory_store.MemoryStore(plugin_config=config, logger=None)
    store.initialize()
    return memory_store, store


def test_atomic_patch_local_profile_preserves_concurrent_fields(profile_store) -> None:
    _memory_store, store = profile_store
    worker_count = 6
    start = threading.Barrier(worker_count + 1)

    def update_field(index: int) -> dict[str, object]:
        start.wait()

        def patch(profile: dict[str, object]) -> dict[str, object]:
            time.sleep(0.01)
            profile[f"field_{index}"] = index
            return profile

        return store.atomic_patch_local_profile(
            group_id="g1",
            user_id="u1",
            patcher=patch,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(update_field, index) for index in range(worker_count)]
        start.wait()
        snapshots = [future.result() for future in futures]

    profile = store.get_local_profile(group_id="g1", user_id="u1")
    assert profile is not None
    for index in range(worker_count):
        assert profile["profile_json"][f"field_{index}"] == index
    assert profile["profile_json"]["revision"] == worker_count
    assert sorted(snapshot["profile_json"]["revision"] for snapshot in snapshots) == list(
        range(1, worker_count + 1)
    )


def test_atomic_patch_local_profile_rejects_stale_revision(profile_store) -> None:
    memory_store, store = profile_store
    first = store.atomic_patch_local_profile(
        group_id="g1",
        user_id="u1",
        patcher=lambda profile: {**profile, "nickname": "first"},
        profile_text="initial profile",
    )
    first_revision = first["profile_json"]["revision"]
    second = store.atomic_patch_local_profile(
        group_id="g1",
        user_id="u1",
        patcher=lambda profile: {**profile, "interest": "music"},
        expected_revision=first_revision,
    )
    patcher_called = False

    def stale_patch(profile: dict[str, object]) -> dict[str, object]:
        nonlocal patcher_called
        patcher_called = True
        return {**profile, "nickname": "stale"}

    with pytest.raises(memory_store.LocalProfileRevisionConflict, match="stale_revision") as exc_info:
        store.atomic_patch_local_profile(
            group_id="g1",
            user_id="u1",
            patcher=stale_patch,
            expected_revision=first_revision,
        )

    assert exc_info.value.code == "stale_revision"
    assert exc_info.value.current_revision == second["profile_json"]["revision"]
    assert second["profile_text"] == "initial profile"
    assert patcher_called is False
    assert store.get_local_profile(group_id="g1", user_id="u1") == second


def test_profile_delete_and_clear_stay_within_profile_scope(profile_store) -> None:
    _memory_store, store = profile_store
    store.upsert_core_profile(user_id="u1", profile_text="core one", profile_json={"core": 1})
    store.upsert_core_profile(user_id="u2", profile_text="core two", profile_json={"core": 2})
    store.upsert_local_profile(
        group_id="g1",
        user_id="u1",
        profile_text="local one",
        profile_json={"local": 1},
    )
    store.upsert_local_profile(
        group_id="g1",
        user_id="u2",
        profile_text="local two",
        profile_json={"local": 2},
    )
    store.upsert_local_profile(
        group_id="g2",
        user_id="u3",
        profile_text="local three",
        profile_json={"local": 3},
    )
    empty_group = store.groups_dir / "empty"
    empty_group.mkdir(parents=True)

    assert store.delete_core_profile("u1") is True
    assert store.get_core_profile("u1") is None
    assert store.get_local_profile(group_id="g1", user_id="u1") is not None
    assert store.delete_local_profile(group_id="g1", user_id="u1") is True
    assert store.get_local_profile(group_id="g1", user_id="u1") is None
    assert store.get_core_profile("u2") is not None
    assert store.get_local_profile(group_id="g2", user_id="u3") is not None

    assert store.clear_all_profiles() == {"core_profiles": 1, "local_profiles": 2}
    assert store.list_core_profiles() == []
    assert store.list_local_profiles("g1") == []
    assert store.list_local_profiles("g2") == []
    assert not (empty_group / "local_user_profiles.db").exists()


@pytest.mark.parametrize(
    ("group_id", "user_id", "message"),
    [("", "u1", "group_id is required"), ("g1", "  ", "user_id is required")],
)
def test_atomic_patch_local_profile_requires_nonempty_ids(
    profile_store,
    group_id: str,
    user_id: str,
    message: str,
) -> None:
    _memory_store, store = profile_store

    with pytest.raises(ValueError, match=message):
        store.atomic_patch_local_profile(
            group_id=group_id,
            user_id=user_id,
            patcher=lambda profile: profile,
        )


def test_profile_generation_fences_writes_started_before_clear(profile_store) -> None:
    memory_store, store = profile_store
    generation = store.get_profile_generation()
    store.clear_all_profiles()

    with pytest.raises(
        memory_store.ProfileGenerationConflict,
        match="profile_generation_changed",
    ):
        store.atomic_patch_local_profile(
            group_id="g1",
            user_id="u1",
            patcher=lambda profile: profile,
            expected_generation=generation,
        )
    with pytest.raises(memory_store.ProfileGenerationConflict):
        store.atomic_patch_core_profile(
            user_id="u1",
            patcher=lambda profile: profile,
            expected_generation=generation,
        )

    assert store.get_local_profile(group_id="g1", user_id="u1") is None
    assert store.get_core_profile("u1") is None


def test_missing_local_profile_read_does_not_create_group_storage(profile_store) -> None:
    _memory_store, store = profile_store

    assert store.get_local_profile(group_id="private_u1", user_id="u1") is None
    assert not (store.groups_dir / "private_u1").exists()


def test_data_transfer_profile_apply_advances_generation(profile_store) -> None:
    _memory_store, store = profile_store
    transfer_mod = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    )
    transfer = object.__new__(transfer_mod.DataTransferService)
    transfer.memory_store = store
    generation = store.get_profile_generation()

    values = {
        "local_user_profiles": [
            {
                "user_id": "u1",
                "profile_text": "imported",
                "profile_json": {},
                "updated_at": 0,
            }
        ]
    }
    transfer._advance_profile_generation_for_values(values)  # noqa: SLF001
    transfer._apply_memory_scope("g1", values, "merge")  # noqa: SLF001

    assert store.get_profile_generation() == generation + 1
    assert store.get_local_profile(group_id="g1", user_id="u1") is not None

    transfer._advance_profile_generation_for_values(  # noqa: SLF001
        {"group_messages": []}
    )
    assert store.get_profile_generation() == generation + 2


def test_single_profile_delete_advances_generation_even_when_missing(profile_store) -> None:
    _memory_store, store = profile_store
    generation = store.get_profile_generation()

    assert store.delete_local_profile(group_id="g1", user_id="missing") is False
    assert store.get_profile_generation() == generation + 1
    assert store.delete_core_profile("missing") is False
    assert store.get_profile_generation() == generation + 2
