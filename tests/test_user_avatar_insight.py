from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ._loader import load_personification_module


avatar = load_personification_module("plugin.personification.core.user_avatar_insight")
avatar_pair = load_personification_module("plugin.personification.core.user_avatar_pair_insight")
memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
persona_routes = load_personification_module("plugin.personification.webui.routes.persona_routes")
persona_service = load_personification_module("plugin.personification.core.persona_service")
profile_service_mod = load_personification_module("plugin.personification.core.profile_service")
safe_image_download = load_personification_module("plugin.personification.core.safe_image_download")
schemas = load_personification_module("plugin.personification.webui.schemas")
user_profile_meta = load_personification_module("plugin.personification.core.user_profile_meta")


def _service(tmp_path: Path):  # noqa: ANN202
    cfg = SimpleNamespace(personification_data_dir=str(tmp_path))
    store = memory_store_mod.MemoryStore(plugin_config=cfg, logger=None)
    store.initialize()
    service = profile_service_mod.ProfileService(store)
    service.upsert_user_profile_meta(
        user_id="10001",
        meta=user_profile_meta.build_user_profile_meta(
            "10001",
            stranger_info={"nickname": "tester"},
            source="test",
            now_ts=1,
        ),
        source="test",
    )
    runtime = SimpleNamespace(
        profile_service=service,
        logger=SimpleNamespace(warning=lambda *_args, **_kwargs: None),
    )
    return service, runtime


def _vision_json(*, kind: str = "acg_character", summary: str = "蓝发动画角色") -> str:
    return json.dumps(
        {
            "asset_kind": kind,
            "subject_count": 1,
            "neutral_summary": summary,
            "acg_candidates": ["角色甲", "作品乙"],
            "contains_text": False,
            "confidence": 0.88,
        },
        ensure_ascii=False,
    )


def test_avatar_analysis_cannot_recreate_profile_after_global_clear(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _download(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return safe_image_download.DownloadedImage(
            b"avatar",
            "image/png",
            "https://cdn.invalid/final",
        )

    async def _analyze(**_kwargs):  # noqa: ANN003, ANN202
        started.set()
        await release.wait()
        return _vision_json(), "route_direct"

    async def _run():  # noqa: ANN202
        task = asyncio.create_task(
            avatar.analyze_user_avatar(
                runtime,
                "10001",
                now_ts=100,
                downloader=_download,
                analyzer=_analyze,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.to_thread(service.memory_store.clear_all_profiles)
        release.set()
        return await task

    result = asyncio.run(_run())

    assert result == {
        "status": "cancelled",
        "reason": "profile_generation_changed",
        "skipped": True,
    }
    assert service.get_core_profile("10001") is None


def test_avatar_analysis_started_before_hot_disable_does_not_commit(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    runtime.plugin_config = SimpleNamespace(personification_persona_enabled=True)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _download(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return safe_image_download.DownloadedImage(
            b"avatar",
            "image/png",
            "https://cdn.invalid/final",
        )

    async def _analyze(**_kwargs):  # noqa: ANN003, ANN202
        started.set()
        await release.wait()
        return _vision_json(), "route_direct"

    async def _run():  # noqa: ANN202
        task = asyncio.create_task(
            avatar.analyze_user_avatar(
                runtime,
                "10001",
                now_ts=100,
                downloader=_download,
                analyzer=_analyze,
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1.0)
        runtime.plugin_config.personification_persona_enabled = False
        release.set()
        return await task

    result = asyncio.run(_run())

    assert result == {
        "status": "cancelled",
        "reason": "profile_generation_changed",
        "skipped": True,
    }
    snapshot = service.get_core_profile("10001")
    assert snapshot is not None
    assert "avatar_analysis" not in snapshot.profile_json.get("qq_profile", {})


def test_avatar_delete_cannot_recreate_profile_after_concurrent_clear(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, _runtime = _service(tmp_path)
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "avatar_analysis": {"status": "success"},
                "avatar_insight": json.loads(_vision_json()),
            },
        },
    )
    original_get = service.get_core_profile

    def _get_then_clear(user_id: str):  # noqa: ANN202
        snapshot = original_get(user_id)
        service.memory_store.clear_all_profiles()
        return snapshot

    monkeypatch.setattr(service, "get_core_profile", _get_then_clear)

    assert avatar.clear_user_avatar_analysis(service, "10001") is False
    assert original_get("10001") is None


def test_hash_unchanged_skips_second_vision_after_check_interval(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    download_calls: list[str] = []
    vision_calls: list[str] = []

    async def _download(url: str, **kwargs):  # noqa: ANN001, ANN202
        download_calls.append(url)
        assert kwargs["max_bytes"] == 5 * 1024 * 1024
        assert kwargs["allowed_mimes"] == {"image/jpeg", "image/png", "image/webp"}
        return safe_image_download.DownloadedImage(b"same-avatar", "image/png", "https://cdn.invalid/final")

    async def _analyze(**kwargs):  # noqa: ANN003, ANN202
        vision_calls.append(kwargs["image_refs"][0])
        assert kwargs["route_name"] == "vision"
        assert kwargs["image_refs"][0].startswith("data:image/png;base64,")
        return _vision_json(), "route_direct"

    first = asyncio.run(
        avatar.analyze_user_avatar(runtime, "10001", now_ts=100, downloader=_download, analyzer=_analyze)
    )
    interval_skip = asyncio.run(
        avatar.analyze_user_avatar(runtime, "10001", now_ts=200, downloader=_download, analyzer=_analyze)
    )
    second = asyncio.run(
        avatar.analyze_user_avatar(
            runtime,
            "10001",
            now_ts=100 + avatar.AVATAR_SUCCESS_CHECK_INTERVAL_SECONDS + 1,
            downloader=_download,
            analyzer=_analyze,
        )
    )

    assert first["status"] == "success"
    assert interval_skip == {"status": "success", "reason": "success_check_interval", "skipped": True}
    assert second == {"status": "unchanged", "reason": "content_hash_unchanged", "skipped": True}
    assert len(download_calls) == 2
    assert len(vision_calls) == 1
    state = avatar.get_user_avatar_state(service, "10001", include_hash=True)
    assert state["analysis"]["status"] == "unchanged"
    assert len(state["analysis"]["content_hash"]) == 64


def test_changed_hash_reruns_vision(tmp_path: Path) -> None:
    _service_obj, runtime = _service(tmp_path)
    payloads = [b"avatar-one", b"avatar-two"]
    vision_calls = 0

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        return safe_image_download.DownloadedImage(payloads.pop(0), "image/jpeg", "https://cdn.invalid/final")

    async def _analyze(**_kwargs):  # noqa: ANN202
        nonlocal vision_calls
        vision_calls += 1
        return _vision_json(), "route_direct"

    first = asyncio.run(avatar.analyze_user_avatar(runtime, "10001", now_ts=10, downloader=_download, analyzer=_analyze))
    second = asyncio.run(
        avatar.analyze_user_avatar(
            runtime,
            "10001",
            now_ts=10 + avatar.AVATAR_SUCCESS_CHECK_INTERVAL_SECONDS + 1,
            downloader=_download,
            analyzer=_analyze,
        )
    )

    assert first["content_hash"] != second["content_hash"]
    assert second["status"] == "success"
    assert vision_calls == 2


def test_force_reruns_vision_for_same_content_hash(tmp_path: Path) -> None:
    _service_obj, runtime = _service(tmp_path)
    vision_calls = 0

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        return safe_image_download.DownloadedImage(b"same-avatar", "image/png", "https://cdn.invalid/final")

    async def _analyze(**_kwargs):  # noqa: ANN202
        nonlocal vision_calls
        vision_calls += 1
        return _vision_json(), "route_direct"

    asyncio.run(avatar.analyze_user_avatar(runtime, "10001", now_ts=10, downloader=_download, analyzer=_analyze))
    forced = asyncio.run(
        avatar.analyze_user_avatar(
            runtime,
            "10001",
            force=True,
            now_ts=11,
            downloader=_download,
            analyzer=_analyze,
        )
    )

    assert forced["status"] == "success"
    assert forced["skipped"] is False
    assert vision_calls == 2


def test_real_person_normalization_discards_sensitive_inferences() -> None:
    normalized = avatar.normalize_avatar_insight(
        {
            "asset_kind": "real_person",
            "subject_count": 1,
            "neutral_summary": "某位年轻女性，情绪低落，可能从事设计工作",
            "acg_candidates": ["现实中的某人"],
            "contains_text": False,
            "confidence": 0.99,
        }
    )
    assert normalized["neutral_summary"] == "真人头像"
    assert normalized["acg_candidates"] == []
    serialized = json.dumps(normalized, ensure_ascii=False)
    for unsafe in ("女性", "年轻", "情绪", "设计工作", "某人"):
        assert unsafe not in serialized


def test_normalization_removes_urls_from_persistable_text() -> None:
    normalized = avatar.normalize_avatar_insight(
        {
            "asset_kind": "acg_character",
            "subject_count": 1,
            "neutral_summary": "候选来源https://private.invalid/avatar",
            "acg_candidates": ["角色甲", "data:image/png;base64,SECRET", "www.invalid/path"],
            "contains_text": False,
            "confidence": 0.6,
        }
    )
    serialized = json.dumps(normalized, ensure_ascii=False)
    assert "角色甲" in serialized
    assert "http" not in serialized.lower()
    assert "data:" not in serialized.lower()
    assert "www." not in serialized.lower()


def test_failed_download_respects_one_hour_cooldown(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    calls = 0

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        nonlocal calls
        calls += 1
        raise RuntimeError("network must stay mocked")

    first = asyncio.run(avatar.analyze_user_avatar(runtime, "10001", now_ts=100, downloader=_download))
    second = asyncio.run(avatar.analyze_user_avatar(runtime, "10001", now_ts=200, downloader=_download))

    assert first["reason"] == "download_failed"
    assert second == {"status": "failed", "reason": "failure_cooldown", "skipped": True}
    assert calls == 1
    assert avatar.get_user_avatar_state(service, "10001")["analysis"]["status"] == "failed"


def test_persistence_and_serialization_never_include_raw_or_image_payload(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        return safe_image_download.DownloadedImage(b"secret-avatar-bytes", "image/webp", "https://cdn.invalid/final")

    async def _analyze(**_kwargs):  # noqa: ANN202
        return _vision_json(summary="安全中性摘要"), "vision_fallback"

    asyncio.run(avatar.analyze_user_avatar(runtime, "10001", now_ts=100, downloader=_download, analyzer=_analyze))
    snapshot = service.get_core_profile("10001")
    persisted = json.dumps(snapshot.profile_json, ensure_ascii=False)
    serialized = json.dumps(avatar.serialize_avatar_state(snapshot.profile_json, include_hash=True), ensure_ascii=False)
    for forbidden in ("secret-avatar-bytes", "data:image", "base64,", "raw_response", "cdn.invalid"):
        assert forbidden not in persisted
        assert forbidden not in serialized


def test_atomic_protocol_merge_and_persona_save_preserve_avatar_fields(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, _runtime = _service(tmp_path)
    insight = avatar.normalize_avatar_insight(json.loads(_vision_json()))
    analysis = {
        "checked_at": 10.0,
        "content_hash": "a" * 64,
        "analyzed_at": 10.0,
        "status": "success",
        "schema_version": 1,
        "route": "route_direct",
    }
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "avatar_analysis": analysis,
                "avatar_insight": insight,
            },
        },
    )
    service.upsert_user_profile_meta(
        user_id="10001",
        meta=user_profile_meta.build_user_profile_meta(
            "10001",
            stranger_info={"nickname": "updated"},
            source="protocol",
            now_ts=20,
        ),
        source="protocol",
    )
    after_protocol = service.get_core_profile("10001").profile_json["qq_profile"]
    assert after_protocol["avatar_analysis"] == analysis
    assert after_protocol["avatar_insight"] == insight

    class _DummyConnection:
        def __enter__(self):  # noqa: ANN204
            return self

        def __exit__(self, *_args):  # noqa: ANN002, ANN204
            return False

        def execute(self, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN201
            return self

        def commit(self) -> None:
            return None

    monkeypatch.setattr(persona_service, "connect_sync", lambda: _DummyConnection())
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=SimpleNamespace(),
        history_max=10,
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        profile_service=service,
    )
    store._save_persona_sync(
        "10001",
        persona_service.PersonaEntry(data="【兴趣领域】：动画", time=30),
        False,
    )
    after_persona = service.get_core_profile("10001").profile_json["qq_profile"]
    assert after_persona == after_protocol


def test_force_can_rerun_same_hash_and_global_inflight_is_deduplicated(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _service_obj, runtime = _service(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _blocked(_runtime, _uid, *, force=False):  # noqa: ANN001, ANN202
        assert force is True
        started.set()
        await release.wait()
        return {"status": "success"}

    monkeypatch.setattr(avatar, "analyze_user_avatar", _blocked)

    async def _run() -> None:
        first = avatar.force_user_avatar_analysis(runtime, "10001")
        second = avatar.force_user_avatar_analysis(runtime, "10001")
        assert first is second
        await started.wait()
        release.set()
        assert await first == {"status": "success"}

    asyncio.run(_run())


def test_force_queues_after_an_inflight_non_force_check(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    _service_obj, runtime = _service(tmp_path)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[bool] = []

    async def _blocked(_runtime, _uid, *, force=False):  # noqa: ANN001, ANN202
        calls.append(bool(force))
        if not force:
            first_started.set()
            await release_first.wait()
        return {"status": "success", "force": bool(force)}

    monkeypatch.setattr(avatar, "analyze_user_avatar", _blocked)

    async def _run() -> None:
        ordinary = avatar.schedule_user_avatar_analysis(runtime, "10001")
        await first_started.wait()
        forced = avatar.force_user_avatar_analysis(runtime, "10001")
        assert forced is not ordinary
        release_first.set()
        assert await ordinary == {"status": "success", "force": False}
        assert await forced == {"status": "success", "force": True}

    asyncio.run(_run())
    assert calls == [False, True]


def test_clear_generation_guard_prevents_late_background_write(tmp_path: Path) -> None:
    service, runtime = _service(tmp_path)
    analysis_started = asyncio.Event()
    release_analysis = asyncio.Event()

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        return safe_image_download.DownloadedImage(b"late-avatar", "image/png", "https://cdn.invalid/final")

    async def _analyze(**_kwargs):  # noqa: ANN202
        analysis_started.set()
        await release_analysis.wait()
        return _vision_json(), "route_direct"

    async def _run() -> dict:
        task = asyncio.create_task(
            avatar.analyze_user_avatar(
                runtime,
                "10001",
                now_ts=100,
                downloader=_download,
                analyzer=_analyze,
            )
        )
        await analysis_started.wait()
        assert avatar.clear_user_avatar_analysis(service, "10001") is False
        release_analysis.set()
        return await task

    result = asyncio.run(_run())
    assert result == {"status": "cancelled", "reason": "profile_generation_changed", "skipped": True}
    assert avatar.get_user_avatar_state(service, "10001")["insight"] == {}


def test_clear_cancels_queued_force_without_starting_a_new_analysis(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, runtime = _service(tmp_path)
    ordinary_started = asyncio.Event()
    calls: list[bool] = []

    async def _blocked(_runtime, _uid, *, force=False):  # noqa: ANN001, ANN202
        calls.append(bool(force))
        ordinary_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(avatar, "analyze_user_avatar", _blocked)

    async def _run() -> None:
        ordinary = avatar.schedule_user_avatar_analysis(runtime, "10001")
        await ordinary_started.wait()
        forced = avatar.force_user_avatar_analysis(runtime, "10001")
        avatar.clear_user_avatar_analysis(service, "10001")
        with pytest.raises(asyncio.CancelledError):
            await forced
        assert ordinary.cancelled()

    asyncio.run(_run())
    assert calls == [False]


def test_bundle_is_normalized_to_reply_runtime_for_vision(tmp_path: Path) -> None:
    _service_obj, inner = _service(tmp_path)
    bundle = SimpleNamespace(reply_processor_deps=SimpleNamespace(runtime=inner))

    async def _download(_url: str, **_kwargs):  # noqa: ANN202
        return safe_image_download.DownloadedImage(b"bundle-avatar", "image/png", "https://cdn.invalid/final")

    async def _analyze(**kwargs):  # noqa: ANN202
        assert kwargs["runtime"] is inner
        return _vision_json(), "route_direct"

    result = asyncio.run(
        avatar.analyze_user_avatar(bundle, "10001", now_ts=100, downloader=_download, analyzer=_analyze)
    )
    assert result["status"] == "success"


def test_agent_tool_returns_current_avatar_evidence_envelope_without_url(tmp_path: Path) -> None:
    service, _runtime = _service(tmp_path)
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "avatar_analysis": {
                    "checked_at": 1,
                    "content_hash": "b" * 64,
                    "analyzed_at": 1,
                    "status": "success",
                    "schema_version": 1,
                    "route": "route_direct",
                },
                "avatar_insight": json.loads(_vision_json()),
            },
        },
    )
    tool = avatar.build_inspect_current_user_avatar_tool(service, "10001")
    result = asyncio.run(tool.handler())
    payload = json.loads(result)
    assert payload["type"] == "personification_evidence_envelope"
    assert payload["available"] is True
    assert any("蓝发动画角色" in claim for claim in payload["allowed_claims"])
    assert any("角色" in claim for claim in payload["allowed_claims"])
    assert any("用户本人" in item for item in payload["forbidden_inferences"])
    assert "头像里像是" in payload["natural_fallback"]
    assert "http" not in result.lower()
    assert "avatar_url" not in result
    assert "b" * 64 not in result
    assert "content_hash_short" not in result
    assert tool.metadata["side_effect"] == "none"
    assert tool.metadata["retryable"] is True
    assert tool.metadata["final_behavior"] == "constrained_persona_output"
    assert "安全头像摘要" not in tool.description
    assert "能否看到或理解自己的头像" in tool.description
    assert "runtime_capability" in tool.metadata["intent_tags"]
    assert tool.metadata["source_kind"] == "first_party_runtime"

    planner_tools = avatar.add_current_user_avatar_planner_metadata([], service, "10001")
    assert planner_tools[0]["name"] == "inspect_current_user_avatar"
    assert "runtime_capability" in planner_tools[0]["intent_tags"]


def test_avatar_tool_is_dynamically_disabled_with_persona_profiles(tmp_path: Path) -> None:
    service, _runtime = _service(tmp_path)
    state = {"enabled": True}
    service._enabled_getter = lambda: state["enabled"]  # noqa: SLF001
    tool = avatar.build_inspect_current_user_avatar_tool(service, "10001")

    assert tool.enabled() is True
    state["enabled"] = False
    assert tool.enabled() is False
    payload = json.loads(asyncio.run(tool.handler()))
    assert payload["type"] == "personification_evidence_envelope"
    assert payload["available"] is False
    assert payload["allowed_claims"] == []
    metadata = avatar.add_current_user_avatar_planner_metadata(
        [{"name": "other"}],
        service,
        "10001",
    )
    assert metadata == [{"name": "other"}]


def test_agent_avatar_unavailable_payload_has_no_hash_or_raw_reference(tmp_path: Path) -> None:
    service, _runtime = _service(tmp_path)
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "avatar_analysis": {
                    "checked_at": 1,
                    "content_hash": "c" * 64,
                    "analyzed_at": 0,
                    "status": "failed",
                    "schema_version": 1,
                    "route": "vision_fallback",
                },
            },
        },
    )

    tool = avatar.build_inspect_current_user_avatar_tool(service, "10001")
    result = asyncio.run(tool.handler())
    payload = json.loads(result)

    assert payload["available"] is False
    assert "content_hash" not in result
    assert "avatar_url" not in result
    assert "data:image" not in result


def test_clear_current_avatar_analysis_also_invalidates_pair_state(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, _runtime = _service(tmp_path)
    invalidated: list[str] = []
    monkeypatch.setattr(
        avatar_pair,
        "clear_user_avatar_pair_analysis",
        lambda user_id: invalidated.append(str(user_id)) or 0,
    )
    assert avatar.clear_user_avatar_analysis(service, "10001") is False
    assert invalidated == ["10001"]


def _admin():  # noqa: ANN202
    return schemas.AdminIdentity(qq="10000", device_id="device", label="test")


def _route(runtime, path: str, method: str):  # noqa: ANN202
    router = persona_routes.build_persona_router(runtime=runtime)
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"route not found: {method} {path}")


def test_avatar_admin_routes_require_permission_keep_diagnostics_and_audit(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, bundle = _service(tmp_path)
    runtime = SimpleNamespace(runtime_bundle=bundle, logger=bundle.logger)
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "email": "private@example.invalid",
                "avatar_analysis": {
                    "status": "success",
                    "content_hash": "c" * 64,
                    "schema_version": 1,
                },
                "avatar_insight": json.loads(_vision_json()),
            },
        },
    )
    audit_actions: list[str] = []
    monkeypatch.setattr(
        persona_routes.webui_audit_log,
        "record",
        lambda **kwargs: audit_actions.append(str(kwargs.get("action", ""))),
    )
    monkeypatch.setattr(persona_routes, "force_user_avatar_analysis", lambda _runtime, _uid: object())
    monkeypatch.setattr(persona_routes, "clear_user_avatar_analysis", lambda _service, _uid: True)

    refresh_route = _route(runtime, "/api/personas/{user_id}/avatar-analysis/refresh", "POST")
    clear_route = _route(runtime, "/api/personas/{user_id}/avatar-analysis", "DELETE")
    assert any(dep.call is persona_routes.require_admin for dep in refresh_route.dependant.dependencies)
    assert any(dep.call is persona_routes.require_admin for dep in clear_route.dependant.dependencies)

    detail_route = _route(runtime, "/api/personas/{user_id}", "GET")
    persona_detail = asyncio.run(detail_route.endpoint("10001", _admin()))
    public_core = persona_detail["core_profile"]
    assert public_core["avatar_analysis"]["content_hash_short"] == "c" * 12
    assert "content_hash" not in public_core["avatar_analysis"]
    assert "avatar_analysis" not in public_core["qq_profile"]
    assert "avatar_insight" not in public_core["qq_profile"]
    assert "email" not in public_core["qq_profile"]
    assert "avatar_analysis" not in public_core["profile_json"]["qq_profile"]

    refreshed = asyncio.run(refresh_route.endpoint("10001", _admin()))
    cleared = asyncio.run(clear_route.endpoint("10001", _admin()))
    assert refreshed["code"] == "avatar_analysis_scheduled"
    assert refreshed["steps"] and refreshed["outcome_unknown"] is False
    assert cleared["code"] == "avatar_analysis_cleared"
    assert cleared["deleted"] is True
    assert audit_actions == ["avatar_analysis_refresh", "avatar_analysis_clear"]
    operation_json = json.dumps([refreshed, cleared], ensure_ascii=False)
    assert "avatar_url" not in operation_json
    assert "raw" not in operation_json.lower()

    missing_runtime = SimpleNamespace(runtime_bundle=SimpleNamespace(), logger=bundle.logger)
    unavailable = _route(
        missing_runtime,
        "/api/personas/{user_id}/avatar-analysis/refresh",
        "POST",
    )
    with pytest.raises(HTTPException) as caught:
        asyncio.run(unavailable.endpoint("10001", _admin()))
    assert caught.value.status_code == 503
    assert caught.value.detail["code"] == "avatar_analysis_unavailable"
    assert caught.value.detail["retryable"] is True

    monkeypatch.setattr(
        persona_routes.webui_audit_log,
        "record",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("audit unavailable")),
    )
    partial_runtime = SimpleNamespace(runtime_bundle=bundle, logger=bundle.logger)
    partial_refresh = _route(
        partial_runtime,
        "/api/personas/{user_id}/avatar-analysis/refresh",
        "POST",
    )
    partial = asyncio.run(partial_refresh.endpoint("10001", _admin()))
    assert partial["code"] == "avatar_analysis_scheduled"
    assert partial["partial"] is True
    assert partial["warnings"]


def test_group_refresh_requires_explicit_bot_and_full_membership_tuple(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    service, bundle = _service(tmp_path)
    refresh_calls: list[tuple[str, str]] = []
    audit_outcomes: list[str] = []

    class _ScopedService:
        async def refresh_group_profile(self, *, group_id: str, user_id: str, force: bool):  # noqa: ANN202
            assert force is True
            refresh_calls.append((group_id, user_id))
            return SimpleNamespace(
                status="succeeded",
                code="ok",
                group_id=group_id,
                user_id=user_id,
                revision=1,
                claim_count=1,
                anchor_count=1,
            )

    class _Bot:
        self_id = "30001"

        def __init__(self) -> None:
            self.groups: list[dict] = []
            self.member_group_id = "20001"

        async def get_group_list(self):  # noqa: ANN202
            return self.groups

        async def get_group_member_info(self, **kwargs):  # noqa: ANN003, ANN202
            assert kwargs["no_cache"] is True
            return {"group_id": self.member_group_id, "user_id": kwargs["user_id"]}

    bot = _Bot()
    runtime = SimpleNamespace(
        runtime_bundle=SimpleNamespace(
            profile_service=service,
            scoped_profile_service=_ScopedService(),
            get_bots=lambda: {"unexpected-key": bot},
        ),
        logger=bundle.logger,
    )
    monkeypatch.setattr(
        persona_routes.webui_audit_log,
        "record",
        lambda **kwargs: audit_outcomes.append(str(kwargs.get("outcome", ""))),
    )
    endpoint = _route(
        runtime,
        "/api/personas/{user_id}/group-refresh",
        "POST",
    ).endpoint

    with pytest.raises(HTTPException) as missing_bot:
        asyncio.run(endpoint("10001", {"group_id": "20001"}, _admin()))
    assert missing_bot.value.status_code == 400

    with pytest.raises(HTTPException) as bot_not_member:
        asyncio.run(
            endpoint(
                "10001",
                {"group_id": "20001", "bot_id": "30001"},
                _admin(),
            )
        )
    assert bot_not_member.value.status_code == 403
    assert refresh_calls == []

    bot.groups = [{"group_id": 20001}]
    bot.member_group_id = "20002"
    with pytest.raises(HTTPException) as stale_member:
        asyncio.run(
            endpoint(
                "10001",
                {"group_id": "20001", "bot_id": "30001"},
                _admin(),
            )
        )
    assert stale_member.value.status_code == 403
    assert refresh_calls == []

    bot.member_group_id = "20001"
    result = asyncio.run(
        endpoint(
            "10001",
            {"group_id": "20001", "bot_id": "30001"},
            _admin(),
        )
    )
    assert result["status"] == "succeeded"
    assert result["bot_id"] == "30001"
    assert refresh_calls == [("20001", "10001")]
    assert audit_outcomes == ["denied", "denied", "denied", "ok"]


def test_webui_renders_avatar_state_buttons_and_persistent_diagnostics() -> None:
    source = (Path(__file__).resolve().parents[1] / "webui" / "static" / "app-identity-policy.js").read_text(encoding="utf-8")
    assert "头像长期画像" in source
    assert "重新分析" in source
    assert "删除分析" in source
    assert "content_hash_short" in source
    assert 'rememberAdminOperation("persona", result' in source
    assert 'renderAdminOperations("persona","画像操作诊断")' in source


def test_webui_persona_detail_connects_scoped_profile_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    admin_source = (root / "webui" / "static" / "app-identity-policy.js").read_text(encoding="utf-8")
    core_source = (root / "webui" / "static" / "app-core.js").read_text(encoding="utf-8")

    assert "/group-refresh" in admin_source
    assert "effective_claims" in admin_source
    assert "personaScopeBotId" in admin_source
    assert "personaScopeGroupId" in admin_source
    assert 'api("/qq/groups")' in core_source


def test_profile_block_and_avatar_tool_have_normal_yaml_agent_parity() -> None:
    root = Path(__file__).resolve().parents[1]
    normal = (root / "handlers" / "reply_pipeline" / "processor.py").read_text(encoding="utf-8")
    pipeline = (root / "handlers" / "reply_pipeline" / "pipeline_context.py").read_text(encoding="utf-8")
    emotion = (root / "handlers" / "reply_pipeline" / "pipeline_emotion.py").read_text(encoding="utf-8")
    yaml = (root / "handlers" / "yaml_pipeline" / "processor.py").read_text(encoding="utf-8")

    assert normal.count("profile_service.build_prompt_block(") == 1
    assert 'system_prompt += f"\\n\\n{user_profile_block}"' in normal
    assert "user_profile_block=user_profile_block" in normal
    assert "build_prompt_block" not in pipeline
    assert 'system_prompt += f"\\n\\n{user_profile_block}"' in yaml
    assert "agent_system_prompt = system_prompt" in yaml
    assert "register_current_user_avatar_tool(" in pipeline
    assert "register_current_user_avatar_tool(agent_tool_registry, profile_service, user_id)" in yaml
    assert "add_current_user_avatar_planner_metadata(" in emotion
    assert "add_current_user_avatar_planner_metadata(" in yaml


def test_profile_prompt_uses_safe_avatar_insight_without_raw_url(tmp_path: Path) -> None:
    service, _runtime = _service(tmp_path)
    service.patch_core_profile(
        user_id="10001",
        patcher=lambda current: {
            **current,
            "qq_profile": {
                **current["qq_profile"],
                "avatar_insight": json.loads(_vision_json()),
            },
        },
    )

    block = service.build_prompt_block(user_id="10001")

    assert "[头像视觉（长期弱证据）]" in block
    assert "蓝发动画角色" in block
    assert "不用于推断真实身份、性别、年龄、性格" in block
    assert "[头像URL]" not in block
    assert "q.qlogo.cn" not in block
