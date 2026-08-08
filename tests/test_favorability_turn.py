from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


favorability_turn = load_personification_module("plugin.personification.core.favorability_turn")
runtime_integrations = load_personification_module("plugin.personification.core.runtime_integrations")


class _Service:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def apply_group_good_atmosphere(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append(("group", args, kwargs))
        return {"status": "applied", "delta": 0.2}

    def apply_user_interesting_chat(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append(("interesting", args, kwargs))
        return {"status": "applied", "delta": 0.12}

    def apply_user_reply_interaction(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append(("reply", args, kwargs))
        return {"status": "applied", "delta": 0.03}


def test_semantic_signals_require_confidence_and_keep_private_group_signal_off() -> None:
    low = favorability_turn.signals_from_semantic_frame(
        SimpleNamespace(
            confidence=0.4,
            group_atmosphere_positive=True,
            interaction_interesting=True,
        ),
        is_private=False,
    )
    private = favorability_turn.signals_from_semantic_frame(
        SimpleNamespace(
            confidence=0.9,
            group_atmosphere_positive=True,
            interaction_interesting=True,
        ),
        is_private=True,
    )

    assert low == favorability_turn.FavorabilityTurnSignals()
    assert private.group_atmosphere_positive is False
    assert private.interaction_interesting is True


def test_legacy_markers_are_compatibility_only_and_never_remain_visible() -> None:
    cleaned, signals = favorability_turn.extract_legacy_favorability_markers(
        "[氛围好] 这句挺有意思 <有趣>"
    )

    assert cleaned == "这句挺有意思"
    assert signals.group_atmosphere_positive is True
    assert signals.interaction_interesting is True


def test_commit_turn_uses_distinct_idempotency_keys_and_skips_group_in_private() -> None:
    service = _Service()
    signals = favorability_turn.FavorabilityTurnSignals(True, True)

    results = favorability_turn.commit_favorability_turn(
        service=service,
        user_id="10001",
        group_id="20001",
        is_private=False,
        is_direct=True,
        is_random_chat=False,
        signals=signals,
        turn_id="turn-1",
    )

    assert len(results) == 3
    assert [call[0] for call in service.calls] == ["group", "interesting", "reply"]
    assert [call[2]["event_id"] for call in service.calls] == [
        "turn-1:group-atmosphere",
        "turn-1:interesting",
        "turn-1:reply",
    ]

    service.calls.clear()
    favorability_turn.commit_favorability_turn(
        service=service,
        user_id="10001",
        group_id="private_10001",
        is_private=True,
        is_direct=True,
        is_random_chat=False,
        signals=signals,
        turn_id="turn-2",
    )
    assert [call[0] for call in service.calls] == ["interesting", "reply"]
    assert service.calls[-1][2]["group_id"] == ""


def test_turn_id_is_stable_and_does_not_embed_raw_identifiers() -> None:
    first = favorability_turn.build_favorability_turn_id(
        trace_id="trace-secret",
        message_id="12345",
        group_id="20001",
        user_id="10001",
    )
    second = favorability_turn.build_favorability_turn_id(
        trace_id="different-retry-trace",
        message_id="12345",
        group_id="20001",
        user_id="10001",
    )

    assert first == second
    assert first.startswith("reply-turn-")
    assert all(raw not in first for raw in ("trace-secret", "12345", "20001", "10001"))

    poke_one = favorability_turn.build_favorability_turn_id(
        trace_id="poke-trace-1",
        group_id="20001",
        user_id="10001",
    )
    poke_two = favorability_turn.build_favorability_turn_id(
        trace_id="poke-trace-2",
        group_id="20001",
        user_id="10001",
    )
    assert poke_one != poke_two
    assert favorability_turn.build_favorability_turn_id(group_id="20001", user_id="10001") == ""


def test_context_block_keeps_relationship_policy_shared() -> None:
    block = favorability_turn.build_favorability_context_block(
        user_level="初见",
        user_attitude="保持基本礼貌",
        group_attitude="群里整体比较融洽",
        is_private=False,
    )

    assert "当前关系表达边界" in block
    assert "保持礼貌和边界感" in block
    assert "群里整体比较融洽" in block


def test_enabled_gate_reads_service_state_dynamically() -> None:
    service = SimpleNamespace(enabled=False)
    gate = runtime_integrations.FavorabilityEnabledGate(service)
    assert bool(gate) is False
    service.enabled = True
    assert bool(gate) is True


def test_normal_and_yaml_paths_share_delivery_aware_commit_helper() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    for source in (normal, yaml):
        assert "commit_favorability_turn(" in source
        assert "extract_legacy_favorability_markers(" in source
        assert "_commit_favorability_if_confirmed()" in source
        assert "on_delivery_confirmed=_confirm_reply_delivery" in source
    assert "service.apply_group_good_atmosphere(" not in normal
    assert "service.apply_user_interesting_chat(" not in normal
    assert "service.apply_user_reply_interaction(" not in normal
