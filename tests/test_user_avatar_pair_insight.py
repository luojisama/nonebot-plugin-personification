from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


pair = load_personification_module("plugin.personification.core.user_avatar_pair_insight")
tool_registry = load_personification_module("plugin.personification.agent.tool_registry")
user_profile_meta = load_personification_module("plugin.personification.core.user_profile_meta")


def _runtime(model: str = "vision-model") -> SimpleNamespace:
    provider = {
        "name": "primary",
        "api_type": "openai",
        "api_url": "https://vision.example/v1",
        "api_key": "secret",
        "model": model,
    }
    return SimpleNamespace(
        plugin_config=SimpleNamespace(
            personification_thinking_mode="none",
            personification_model_overrides={},
        ),
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
        get_configured_api_providers=lambda: [dict(provider)],
    )


def _candidates(*, reversed_order: bool = False) -> list[dict[str, str]]:
    values = [
        {"user_id": "10001", "label": "甲"},
        {"user_id": "10002", "label": "乙"},
    ]
    return list(reversed(values)) if reversed_order else values


def _event(*, group: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        message_type="group" if group else "private",
        group_id=20001 if group else None,
        user_id=10001,
    )


def _assessment(
    relation: str = "coordinated_pair",
    *,
    asset_kinds: list[str] | None = None,
) -> str:
    tags = {
        "coordinated_pair": ["complementary_composition", "matching_palette"],
        "same_character": ["same_character_features"],
        "same_series": ["same_series_style"],
        "unrelated": ["no_clear_link"],
        "uncertain": ["insufficient_detail"],
        "near_duplicate": ["near_identical_composition"],
    }
    return json.dumps(
        {
            "relation": relation,
            "asset_kinds": asset_kinds or ["illustration", "illustration"],
            "evidence_tags": tags[relation],
            "confidence": 0.95,
        },
        ensure_ascii=False,
    )


@pytest.fixture(autouse=True)
def _clear_pair_state():
    pair._clear_pair_cache_for_testing()
    yield
    pair._clear_pair_cache_for_testing()


def test_tool_build_scope_enum_handler_validation_and_metadata(monkeypatch) -> None:  # noqa: ANN001
    bot = SimpleNamespace(self_id="90001")
    assert pair.build_group_user_avatar_pair_insight_tool(
        runtime=_runtime(), bot=bot, event=_event(group=False), candidates=_candidates()
    ) is None
    assert pair.build_group_user_avatar_pair_insight_tool(
        runtime=_runtime(), bot=bot, event=_event(), candidates=_candidates()[:1]
    ) is None

    candidates = [
        {"user_id": str(10001 + index), "label": f"候选{index + 1}"}
        for index in range(7)
    ]
    tool = pair.build_group_user_avatar_pair_insight_tool(
        runtime=_runtime(),
        bot=bot,
        event=_event(),
        candidates=candidates,
    )
    assert tool is not None
    schema = tool.parameters
    assert schema["additionalProperties"] is False
    assert schema["properties"]["left_label"]["enum"] == [f"候选{index + 1}" for index in range(6)]
    assert schema["properties"]["right_label"]["enum"] == [f"候选{index + 1}" for index in range(6)]
    assert tool.per_session_quota == 1
    assert tool.metadata["read_only"] is True
    assert tool.metadata["side_effect"] == "none"
    assert tool.metadata["retryable"] is True
    assert tool.metadata["requires_network"] is True
    assert tool.metadata["final_behavior"] == "constrained_persona_output"

    calls = 0

    async def _compare(**_kwargs):  # noqa: ANN202
        nonlocal calls
        calls += 1
        return pair._safe_pair_payload("候选1", "候选2", json.loads(_assessment()))

    monkeypatch.setattr(pair, "analyze_group_user_avatar_pair", _compare)

    async def _run() -> tuple[dict, dict, dict]:
        invalid_enum = json.loads(await tool.handler(left_label="不存在", right_label="候选2"))
        invalid_distinct = json.loads(await tool.handler(left_label="候选1", right_label="候选1"))
        valid = json.loads(await tool.handler(left_label="候选1", right_label="候选2"))
        repeated = json.loads(await tool.handler(left_label="候选2", right_label="候选3"))
        return invalid_enum, invalid_distinct, valid, repeated

    invalid_enum, invalid_distinct, valid, repeated = asyncio.run(_run())
    assert invalid_enum["type"] == "personification_evidence_envelope"
    assert invalid_enum["available"] is False
    assert invalid_distinct["available"] is False
    assert valid["available"] is True
    assert repeated["available"] is False
    assert calls == 1


def test_registration_api_only_registers_for_valid_group_scope() -> None:
    registry = tool_registry.ToolRegistry()
    bot = SimpleNamespace(self_id="90001")
    assert pair.register_group_user_avatar_pair_insight_tool(
        registry,
        runtime=_runtime(),
        bot=bot,
        event=_event(group=False),
        candidates=_candidates(),
    ) is False
    assert registry.get("inspect_group_user_avatar_pair") is None
    assert pair.register_group_user_avatar_pair_insight_tool(
        registry,
        runtime=_runtime(),
        bot=bot,
        event=_event(),
        candidates=_candidates(),
    ) is True
    assert registry.get("inspect_group_user_avatar_pair") is not None


def test_candidate_builder_prioritizes_sender_mentions_reply_and_recent_members() -> None:
    event = SimpleNamespace(
        message_type="group",
        group_id=20001,
        user_id=10001,
        sender=SimpleNamespace(card="当前甲", nickname="甲"),
        message=[{"type": "at", "data": {"qq": "10002"}}],
        reply=SimpleNamespace(
            sender=SimpleNamespace(user_id=10003, nickname="引用乙"),
        ),
    )
    recent = [
        {"user_id": "90001", "speaker": "机器人", "source_kind": "bot_reply", "is_bot": True},
        {"user_id": "10002", "speaker": "提及丙", "source_kind": "group_message"},
        {"user_id": "10004", "speaker": "同名", "source_kind": "group_message"},
        {"user_id": "10005", "speaker": "同名", "source_kind": "group_message"},
        {"user_id": "10006", "speaker": "成员己", "source_kind": "group_message"},
        {"user_id": "10007", "speaker": "成员庚", "source_kind": "group_message"},
    ]

    candidates = pair.build_avatar_pair_candidates(
        event=event,
        current_user_id="10001",
        current_user_label="甲",
        bot_self_id="90001",
        batched_events=[{"user_id": "10001", "sender_name": "当前甲"}],
        recent_messages=recent,
    )

    assert [item["user_id"] for item in candidates[:3]] == ["10001", "10002", "10003"]
    assert candidates[1]["label"] == "提及丙"
    assert candidates[2]["label"] == "引用乙"
    assert len(candidates) == 6
    assert "90001" not in {item["user_id"] for item in candidates}
    assert len({item["label"] for item in candidates}) == len(candidates)
    assert all(item["user_id"] not in item["label"] for item in candidates)


def test_candidate_builder_requires_group_and_two_distinct_members() -> None:
    private_event = SimpleNamespace(message_type="private", user_id=10001)
    group_event = SimpleNamespace(
        message_type="group",
        group_id=20001,
        user_id=10001,
        sender=SimpleNamespace(nickname="甲"),
        message=[],
        reply=None,
    )

    assert pair.build_avatar_pair_candidates(
        event=private_event,
        current_user_id="10001",
        current_user_label="甲",
    ) == []
    assert pair.build_avatar_pair_candidates(
        event=group_event,
        current_user_id="10001",
        current_user_label="甲",
    ) == []


def test_policy_filter_removes_blocked_candidates_before_tool_schema() -> None:
    async def _authorize(user_id: str):
        allowed = user_id != "10002"
        return SimpleNamespace(blocked=not allowed, allow_context_read=allowed)

    filtered = asyncio.run(
        pair.filter_avatar_candidates_by_policy(_candidates(), _authorize)
    )

    assert filtered == [{"user_id": "10001", "label": "甲"}]


def test_non_sender_membership_failure_is_fail_closed_before_download() -> None:
    async def _membership(_bot, group_id, user_id):  # noqa: ANN001, ANN202
        assert group_id == "20001"
        assert user_id == "10002"
        return None

    async def _download(*_args, **_kwargs):  # noqa: ANN202
        raise AssertionError("membership failure must stop before avatar download")

    result = asyncio.run(
        pair.analyze_group_user_avatar_pair(
            runtime=_runtime(),
            bot=SimpleNamespace(self_id="90001"),
            group_id="20001",
            sender_user_id="10001",
            left_user_id="10001",
            left_label="甲",
            right_user_id="10002",
            right_label="乙",
            membership_getter=_membership,
            downloader=_download,
        )
    )
    assert result["type"] == "personification_evidence_envelope"
    assert result["available"] is False
    assert "这个群" in result["natural_fallback"]


def test_exact_sanitized_bytes_skip_vision_and_use_only_deterministic_avatar_urls(monkeypatch) -> None:  # noqa: ANN001
    download_calls: list[tuple[str, dict]] = []
    sanitize_calls: list[tuple[bytes, str]] = []

    async def _membership(_bot, _group_id, user_id):  # noqa: ANN001, ANN202
        return {"user_id": int(user_id), "group_id": 20001}

    async def _download(url: str, **kwargs):  # noqa: ANN001, ANN202
        download_calls.append((url, dict(kwargs)))
        return SimpleNamespace(content=b"same-raw-avatar", content_type="image/png")

    def _sanitize(content: bytes, content_type: str):  # noqa: ANN202
        sanitize_calls.append((content, content_type))
        return b"same-sanitized-avatar", "image/png", 128, 128, ".png"

    async def _vision(**_kwargs):  # noqa: ANN202
        raise AssertionError("exact sanitized bytes must not call Vision")

    monkeypatch.setattr(pair, "sanitize_image", _sanitize)
    result = asyncio.run(
        pair.analyze_group_user_avatar_pair(
            runtime=_runtime(),
            bot=SimpleNamespace(self_id="90001"),
            group_id="20001",
            sender_user_id="10001",
            left_user_id="10001",
            left_label="甲",
            right_user_id="10002",
            right_label="乙",
            membership_getter=_membership,
            downloader=_download,
            analyzer=_vision,
        )
    )

    assert result["available"] is True
    assert "相同或近乎相同" in result["natural_fallback"]
    assert [item[0] for item in download_calls] == [
        user_profile_meta.qq_avatar_url("10001"),
        user_profile_meta.qq_avatar_url("10002"),
    ]
    assert all(item[1]["max_bytes"] == 5 * 1024 * 1024 for item in download_calls)
    assert all(item[1]["allowed_mimes"] == {"image/jpeg", "image/png", "image/webp"} for item in download_calls)
    assert len(sanitize_calls) == 2
    assert set(result) == {
        "type",
        "available",
        "allowed_claims",
        "forbidden_inferences",
        "confidence",
        "natural_fallback",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    for forbidden in ("10001", "10002", "q.qlogo.cn", "sha256", "raw", "relation"):
        assert forbidden not in serialized


def test_strict_normalization_thresholds_and_real_person_boundary() -> None:
    with pytest.raises(ValueError):
        pair.normalize_avatar_pair_assessment({**json.loads(_assessment()), "free_text": "不允许"})
    with pytest.raises(ValueError):
        pair.normalize_avatar_pair_assessment(
            {**json.loads(_assessment()), "evidence_tags": ["not_allowed"]}
        )
    with pytest.raises(ValueError):
        pair.normalize_avatar_pair_assessment(
            {**json.loads(_assessment()), "confidence": float("nan")}
        )
    with pytest.raises(ValueError):
        pair._parse_pair_response(
            json.dumps(
                {
                    "relation": "near_duplicate",
                    "asset_kinds": ["illustration", "illustration"],
                    "evidence_tags": ["exact_bytes"],
                    "confidence": 1.0,
                }
            )
        )
    assert "exact_bytes" not in pair._PAIR_SCHEMA["properties"]["evidence_tags"]["items"]["enum"]

    weak = pair.normalize_avatar_pair_assessment(
        {
            "relation": "coordinated_pair",
            "asset_kinds": ["illustration", "illustration"],
            "evidence_tags": ["matching_palette"],
            "confidence": 0.99,
        }
    )
    assert weak["relation"] == "uncertain"
    assert "insufficient_detail" in weak["evidence_tags"]

    real_person = pair.normalize_avatar_pair_assessment(
        {
            "relation": "same_character",
            "asset_kinds": ["real_person", "illustration"],
            "evidence_tags": ["same_character_features"],
            "confidence": 0.99,
        }
    )
    assert real_person["relation"] == "uncertain"
    assert "real_person_present" in real_person["evidence_tags"]
    payload = pair._safe_pair_payload("甲", "乙", real_person)
    assert "同一虚构角色" not in payload["natural_fallback"]
    assert any("身份" in item and "same-person" in item for item in payload["forbidden_inferences"])


def test_normal_and_yaml_agent_paths_register_pair_tool_per_turn() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "pipeline_context.py").read_text(encoding="utf-8")
    processor = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    assert "register_group_user_avatar_pair_insight_tool(" in normal
    assert "avatar_pair_candidates=avatar_pair_candidates" in processor
    assert "filter_avatar_candidates_by_policy(" in processor
    assert "constrained_persona_output" in pair.build_group_user_avatar_pair_insight_tool(
        runtime=_runtime(),
        bot=SimpleNamespace(self_id="90001"),
        event=_event(),
        candidates=_candidates(),
    ).metadata["final_behavior"]
    assert "register_group_user_avatar_pair_insight_tool(" in yaml
    assert "resolved_avatar_pair_candidates" in yaml


def test_cache_is_order_canonical_and_invalidates_on_hash_prompt_and_provider(monkeypatch) -> None:  # noqa: ANN001
    payloads = {"10001": b"avatar-a", "10002": b"avatar-b"}
    analyzer_calls = 0

    async def _membership(_bot, _group_id, user_id):  # noqa: ANN001, ANN202
        return {"user_id": int(user_id)}

    async def _download(url: str, **_kwargs):  # noqa: ANN202
        user_id = "10001" if "dst_uin=10001" in url else "10002"
        return SimpleNamespace(content=payloads[user_id], content_type="image/png")

    def _sanitize(content: bytes, _content_type: str):  # noqa: ANN202
        return b"sanitized:" + content, "image/png", 128, 128, ".png"

    async def _analyze(**kwargs):  # noqa: ANN003, ANN202
        nonlocal analyzer_calls
        analyzer_calls += 1
        assert len(kwargs["image_refs"]) == 2
        return _assessment(), "route_direct"

    monkeypatch.setattr(pair, "get_group_member_info", _membership)
    monkeypatch.setattr(pair, "download_public_image", _download)
    monkeypatch.setattr(pair, "sanitize_image", _sanitize)
    monkeypatch.setattr(pair, "analyze_images_with_primary_route_joint_only", _analyze)

    async def _call(runtime: SimpleNamespace, *, reverse: bool = False) -> dict:
        tool = pair.build_group_user_avatar_pair_insight_tool(
            runtime=runtime,
            bot=SimpleNamespace(self_id="90001"),
            event=_event(),
            candidates=_candidates(reversed_order=reverse),
        )
        assert tool is not None
        if reverse:
            return json.loads(await tool.handler(left_label="乙", right_label="甲"))
        return json.loads(await tool.handler(left_label="甲", right_label="乙"))

    async def _run() -> None:
        assert (await _call(_runtime()))["available"] is True
        assert (await _call(_runtime(), reverse=True))["available"] is True
        assert analyzer_calls == 1

        payloads["10002"] = b"avatar-b-changed"
        assert (await _call(_runtime()))["available"] is True
        assert analyzer_calls == 2

        monkeypatch.setattr(pair, "_PAIR_PROMPT_FINGERPRINT", "changed-prompt")
        assert (await _call(_runtime()))["available"] is True
        assert analyzer_calls == 3

        assert (await _call(_runtime("other-vision-model")))["available"] is True
        assert analyzer_calls == 4

    asyncio.run(_run())


def test_pair_analysis_persists_visual_evidence_only_after_policy_recheck(monkeypatch) -> None:  # noqa: ANN001
    persisted: list[dict] = []
    authorization_calls: dict[str, int] = {"10001": 0, "10002": 0}

    async def _membership(_bot, _group_id, user_id):  # noqa: ANN001, ANN202
        return {"user_id": int(user_id)}

    async def _download(url: str, **_kwargs):  # noqa: ANN202
        content = b"left" if "dst_uin=10001" in url else b"right"
        return SimpleNamespace(content=content, content_type="image/png")

    def _sanitize(content: bytes, _content_type: str):  # noqa: ANN202
        return b"sanitized:" + content, "image/png", 128, 128, ".png"

    async def _analyze(**_kwargs):  # noqa: ANN202
        return _assessment(), "route_direct"

    async def _allow(user_id: str):
        authorization_calls[user_id] += 1
        return SimpleNamespace(blocked=False, allow_context_read=True)

    monkeypatch.setattr(pair, "get_group_member_info", _membership)
    monkeypatch.setattr(pair, "download_public_image", _download)
    monkeypatch.setattr(pair, "sanitize_image", _sanitize)
    monkeypatch.setattr(pair, "analyze_images_with_primary_route_joint_only", _analyze)
    monkeypatch.setattr(
        pair,
        "record_avatar_relation_evidence",
        lambda **kwargs: persisted.append(dict(kwargs)) or True,
    )

    payload = asyncio.run(
        pair.analyze_group_user_avatar_pair(
            runtime=_runtime(),
            bot=SimpleNamespace(self_id="90001"),
            group_id="20001",
            sender_user_id="10001",
            left_user_id="10001",
            left_label="甲",
            right_user_id="10002",
            right_label="乙",
            policy_authorizer=_allow,
        )
    )

    assert payload["available"] is True
    assert len(persisted) == 1
    assert persisted[0]["relation"] == "coordinated_pair"
    assert set(persisted[0]["avatar_hashes"]) == {"10001", "10002"}
    assert authorization_calls == {"10001": 2, "10002": 2}


def test_pair_analysis_drops_result_if_target_becomes_blocked_before_persist(monkeypatch) -> None:  # noqa: ANN001
    target_calls = 0

    async def _membership(_bot, _group_id, user_id):  # noqa: ANN001, ANN202
        return {"user_id": int(user_id)}

    async def _download(url: str, **_kwargs):  # noqa: ANN202
        content = b"left" if "dst_uin=10001" in url else b"right"
        return SimpleNamespace(content=content, content_type="image/png")

    async def _authorize(user_id: str):
        nonlocal target_calls
        if user_id == "10002":
            target_calls += 1
            allowed = target_calls == 1
        else:
            allowed = True
        return SimpleNamespace(blocked=not allowed, allow_context_read=allowed)

    monkeypatch.setattr(pair, "get_group_member_info", _membership)
    monkeypatch.setattr(pair, "download_public_image", _download)
    monkeypatch.setattr(
        pair,
        "sanitize_image",
        lambda content, _mime: (b"sanitized:" + content, "image/png", 128, 128, ".png"),
    )
    monkeypatch.setattr(
        pair,
        "analyze_images_with_primary_route_joint_only",
        lambda **_kwargs: asyncio.sleep(0, result=(_assessment(), "route_direct")),
    )
    monkeypatch.setattr(
        pair,
        "record_avatar_relation_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("blocked evidence must not persist")),
    )

    payload = asyncio.run(
        pair.analyze_group_user_avatar_pair(
            runtime=_runtime(),
            bot=SimpleNamespace(self_id="90001"),
            group_id="20001",
            sender_user_id="10001",
            left_user_id="10001",
            left_label="甲",
            right_user_id="10002",
            right_label="乙",
            policy_authorizer=_authorize,
        )
    )

    assert payload["available"] is False
    assert payload["allowed_claims"] == []


def test_failure_cooldown_inflight_dedup_and_clear_generation_guard(monkeypatch) -> None:  # noqa: ANN001
    analyzer_calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def _membership(_bot, _group_id, user_id):  # noqa: ANN001, ANN202
        return {"user_id": int(user_id)}

    async def _download(url: str, **_kwargs):  # noqa: ANN202
        value = b"left" if "dst_uin=10001" in url else b"right"
        return SimpleNamespace(content=value, content_type="image/png")

    def _sanitize(content: bytes, _content_type: str):  # noqa: ANN202
        return b"sanitized:" + content, "image/png", 128, 128, ".png"

    async def _analyze(**_kwargs):  # noqa: ANN202
        nonlocal analyzer_calls
        analyzer_calls += 1
        started.set()
        await release.wait()
        return _assessment(), "route_direct"

    monkeypatch.setattr(pair, "get_group_member_info", _membership)
    monkeypatch.setattr(pair, "download_public_image", _download)
    monkeypatch.setattr(pair, "sanitize_image", _sanitize)
    monkeypatch.setattr(pair, "analyze_images_with_primary_route_joint_only", _analyze)

    def _tool():  # noqa: ANN202
        return pair.build_group_user_avatar_pair_insight_tool(
            runtime=_runtime(),
            bot=SimpleNamespace(self_id="90001"),
            event=_event(),
            candidates=_candidates(),
        )

    async def _run_inflight_and_clear() -> None:
        first_tool = _tool()
        second_tool = _tool()
        assert first_tool is not None and second_tool is not None
        first = asyncio.create_task(first_tool.handler(left_label="甲", right_label="乙"))
        second = asyncio.create_task(second_tool.handler(left_label="乙", right_label="甲"))
        await started.wait()
        assert analyzer_calls == 1
        assert pair.clear_user_avatar_pair_analysis("10001") >= 1
        first_payload, second_payload = [json.loads(item) for item in await asyncio.gather(first, second)]
        assert first_payload["available"] is False
        assert second_payload["available"] is False

        release.set()
        next_tool = _tool()
        assert next_tool is not None
        assert json.loads(await next_tool.handler(left_label="甲", right_label="乙"))["available"] is True
        assert analyzer_calls == 2

    asyncio.run(_run_inflight_and_clear())

    pair._clear_pair_cache_for_testing()
    invalid_calls = 0

    async def _invalid(**_kwargs):  # noqa: ANN202
        nonlocal invalid_calls
        invalid_calls += 1
        return "not-json", "route_direct"

    monkeypatch.setattr(pair, "analyze_images_with_primary_route_joint_only", _invalid)

    async def _run_failure_cache() -> None:
        for _index in range(2):
                tool = _tool()
                assert tool is not None
                payload = json.loads(await tool.handler(left_label="甲", right_label="乙"))
                assert payload["type"] == "personification_evidence_envelope"
                assert payload["available"] is False

    asyncio.run(_run_failure_cache())
    assert invalid_calls == 1
    assert pair.AVATAR_PAIR_FAILURE_TTL_SECONDS == 5 * 60


def test_pair_cache_is_bounded_lru() -> None:
    assessment = json.loads(_assessment())
    for index in range(pair.AVATAR_PAIR_CACHE_MAX_ENTRIES + 1):
        pair._cache_set(
            (index,),
            users=frozenset({str(index)}),
            assessment=assessment,
            ttl=60,
        )
    assert len(pair._PAIR_CACHE) == 256
    assert (0,) not in pair._PAIR_CACHE
