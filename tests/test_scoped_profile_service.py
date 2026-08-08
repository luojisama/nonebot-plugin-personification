from __future__ import annotations

import asyncio
import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


db = load_personification_module("plugin.personification.core.db")
memory_store_mod = load_personification_module("plugin.personification.core.memory_store")
persona_service = load_personification_module("plugin.personification.core.persona_service")
profile_service_mod = load_personification_module("plugin.personification.core.profile_service")
scoped_profile = load_personification_module("plugin.personification.core.scoped_profile")
scoped_service = load_personification_module(
    "plugin.personification.core.scoped_profile_service"
)


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(str(message))

    def warning(self, message: str) -> None:
        self.messages.append(str(message))


class _Caller:
    def __init__(self, *, claims_factory=None) -> None:  # noqa: ANN001
        self.claims_factory = claims_factory
        self.prompts: list[str] = []

    async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001, ANN202
        assert tools == []
        assert use_builtin_search is False
        prompt = str(messages[-1]["content"])
        self.prompts.append(prompt)
        payload = json.loads(prompt)
        anchor_ids = [
            int(item["anchor_row_id"])
            for item in payload["evidence_windows"]
        ]
        claims = (
            self.claims_factory(anchor_ids)
            if callable(self.claims_factory)
            else [
                {
                    "key": "communication_style",
                    "value": "在本群常用短句接话",
                    "confidence": 0.88,
                    "evidence_anchor_row_ids": anchor_ids,
                },
                {
                    "key": "occupation",
                    "value": "从群聊猜出的职业，必须丢弃",
                    "confidence": 0.99,
                    "evidence_anchor_row_ids": anchor_ids,
                },
            ]
        )
        return SimpleNamespace(content=json.dumps({"claims": claims}, ensure_ascii=False))


def _runtime(  # noqa: ANN202
    tmp_path: Path,
    *,
    enabled=True,  # noqa: ANN001
    caller=None,  # noqa: ANN001
    max_concurrency: int = 4,
):
    db_path = db.init_db_sync(tmp_path)
    config = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_memory_enabled=True,
        personification_memory_palace_enabled=False,
    )
    store = memory_store_mod.MemoryStore(config, logger=None)
    store.initialize()
    profiles = profile_service_mod.ProfileService(
        store,
        enabled_getter=lambda: bool(enabled),
    )
    logger = _Logger()
    service = scoped_service.ScopedProfileService(
        profile_service=profiles,
        tool_caller=caller or _Caller(),
        logger=logger,
        enabled=lambda: bool(enabled),
        auto_threshold=1,
        settle_after_group_rows=0,
        max_concurrency=max_concurrency,
        db_path=db_path,
        clock=lambda: 500.0,
    )
    return db_path, store, profiles, service, logger


def _insert(
    db_path: Path,
    *,
    group_id: str = "20001",
    user_id: str,
    content: str,
    message_id: str,
    timestamp: float,
    reply_to_msg_id: str = "",
    mentioned_ids: str = "[]",
    thread_id: str = "thread-1",
    source_kind: str = "user",
) -> int:
    with db.connect_sync(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO group_messages(
                group_id,user_id,content,is_bot,reply_to_msg_id,mentioned_ids,
                message_id,thread_id,source_kind,timestamp
            ) VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                user_id,
                content,
                reply_to_msg_id or None,
                mentioned_ids,
                message_id,
                thread_id,
                source_kind,
                timestamp,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def test_scoped_generation_persists_only_contextual_claims_and_hashed_refs(tmp_path) -> None:  # noqa: ANN001
    caller = _Caller()
    db_path, _store, profiles, service, _logger = _runtime(tmp_path, caller=caller)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="长期喜欢画画",
        profile_json={
            "structured": {"interests": "画画", "occupation": "学生"},
            "user_corrections": {"职业": "设计师"},
        },
    )
    _insert(
        db_path,
        user_id="10002",
        content="你今天也来接梗了",
        message_id="context-before",
        timestamp=1,
    )
    anchor = _insert(
        db_path,
        user_id="10001",
        content="SELF_RAW_SHOULD_NOT_PERSIST，今天就短短回一句",
        message_id="anchor",
        timestamp=2,
    )
    _insert(
        db_path,
        user_id="10003",
        content="CONTEXT_RAW_SHOULD_NOT_PERSIST",
        message_id="context-after",
        timestamp=3,
        reply_to_msg_id="anchor",
    )

    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )

    assert result.status == "succeeded"
    assert result.revision == 1
    assert result.anchor_count == 1
    local = profiles.get_local_profile(group_id="20001", user_id="10001")
    assert local is not None
    assert local.profile_json["schema_version"] == 2
    assert local.profile_json["scope"] == {"kind": "group", "group_id": "20001"}
    claims = {claim["key"]: claim for claim in local.profile_json["claims"]}
    assert set(claims) == {"communication_style"}
    assert claims["communication_style"]["value"] == "在本群常用短句接话"
    assert claims["communication_style"]["source"] == "evidence_derived"
    serialized = json.dumps(local.profile_json, ensure_ascii=False)
    assert "SELF_RAW_SHOULD_NOT_PERSIST" not in serialized
    assert "CONTEXT_RAW_SHOULD_NOT_PERSIST" not in serialized
    assert "content_sha256" in serialized
    prompt_payload = json.loads(caller.prompts[0])
    prompt_window = prompt_payload["evidence_windows"][0]
    assert prompt_window["anchor_row_id"] == anchor
    assert prompt_window["anchor"]["actor"] == "SELF"
    assert prompt_window["after"][0]["relation"] == "reply"
    prompt_block = profiles.build_prompt_block(user_id="10001", group_id="20001")
    assert "[全局稳定信息]" in prompt_block
    assert "职业/学业: 设计师" in prompt_block
    assert "[当前群差异]" in prompt_block
    assert "沟通风格: 在本群常用短句接话" in prompt_block
    assert "他人发言不等于用户自述" in prompt_block


def test_same_thread_only_claim_confidence_is_capped(tmp_path) -> None:  # noqa: ANN001
    caller = _Caller(
        claims_factory=lambda anchors: [
            {
                "key": "group_role",
                "value": "偶尔接话的群员",
                "confidence": 0.95,
                "evidence_anchor_row_ids": anchors,
            }
        ]
    )
    db_path, _store, profiles, service, _logger = _runtime(tmp_path, caller=caller)
    _insert(
        db_path,
        user_id="10002",
        content="同 thread 语境",
        message_id="before",
        timestamp=1,
    )
    _insert(
        db_path,
        user_id="10001",
        content="目标用户发言",
        message_id="anchor",
        timestamp=2,
    )

    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )

    assert result.status == "succeeded"
    local = profiles.get_local_profile(group_id="20001", user_id="10001")
    assert local is not None
    claim = local.profile_json["claims"][0]
    assert claim["key"] == "group_role"
    assert claim["confidence"] == 0.6


def test_confirmed_global_claim_cannot_be_overridden_by_group_delta(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="",
        profile_json={
            "structured": {"communication_style": "旧值"},
            "user_corrections": {"沟通风格": "用户确认的简短直说"},
        },
    )
    global_doc = scoped_profile.build_global_profile_document(
        profiles.get_core_profile("10001").profile_json
    )
    group_doc = scoped_profile.build_group_profile_document(
        "20001",
        claims=[
            {
                "key": "communication_style",
                "value": "群内推断的长篇表达",
                "source": "evidence_derived",
                "confidence": 0.9,
            }
        ],
        global_document=global_doc,
    )
    profiles.upsert_local_profile(
        group_id="20001",
        user_id="10001",
        profile_text="群内推断的长篇表达",
        profile_json=group_doc,
    )

    effective = {
        claim["key"]: claim
        for claim in profiles.get_effective_claims(user_id="10001", group_id="20001")
    }
    assert effective["communication_style"]["value"] == "用户确认的简短直说"
    prompt = profiles.build_prompt_block(user_id="10001", group_id="20001")
    assert "用户确认的简短直说" in prompt
    assert "群内推断的长篇表达" not in prompt
    assert "旧值" not in prompt


def test_profile_prompt_is_disabled_with_persona_switch(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, service, _logger = _runtime(tmp_path, enabled=False)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="不应注入",
        profile_json={"structured": {"interests": "测试"}},
    )

    assert profiles.build_prompt_block(user_id="10001", group_id="20001") == ""
    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )
    assert result.code == "disabled"


def test_effective_profile_ignores_local_document_for_another_group(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="global",
        profile_json={"structured": {"communication_style": "global style"}},
    )
    wrong_scope = scoped_profile.build_group_profile_document(
        "20002",
        claims=[
            {
                "key": "communication_style",
                "value": "wrong group style",
                "source": "group_generated",
            }
        ],
    )
    profiles.upsert_local_profile(
        group_id="20001",
        user_id="10001",
        profile_text="wrong group style",
        profile_json=wrong_scope,
    )

    claims = {
        claim["key"]: claim
        for claim in profiles.get_effective_claims(user_id="10001", group_id="20001")
    }
    assert claims["communication_style"]["value"] == "global style"
    assert "wrong group style" not in profiles.build_prompt_block(
        user_id="10001",
        group_id="20001",
    )


def test_persona_save_embeds_versioned_global_claim_document(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=object(),
        history_max=5,
        logger=_Logger(),
        profile_service=profiles,
    )
    entry = persona_service.PersonaEntry(
        data="【职业推测】：学生\n【沟通风格】：简短",
        time=500,
    )
    store._save_persona_sync(  # noqa: SLF001
        "10001",
        entry,
        False,
        corrections={"职业": "设计师"},
    )

    core = profiles.get_core_profile("10001")
    assert core is not None
    document = core.profile_json["scoped_profile"]
    assert document["schema_version"] == 2
    assert document["revision"] == 1
    claims = {claim["key"]: claim for claim in document["claims"]}
    assert claims["occupation"]["value"] == "设计师"
    assert claims["occupation"]["source"] == "user_confirmed"
    assert claims["communication_style"]["source"] == "global_generated"


def test_persona_regeneration_replaces_prior_generated_claim_value(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=object(),
        history_max=5,
        logger=_Logger(),
        profile_service=profiles,
    )
    store._save_persona_sync(  # noqa: SLF001
        "10001",
        persona_service.PersonaEntry(data="【沟通风格】：旧的长句", time=500),
        False,
    )
    store._save_persona_sync(  # noqa: SLF001
        "10001",
        persona_service.PersonaEntry(data="【沟通风格】：新的短句", time=501),
        False,
    )

    core = profiles.get_core_profile("10001")
    assert core is not None
    document = core.profile_json["scoped_profile"]
    claims = {claim["key"]: claim for claim in document["claims"]}
    assert document["revision"] == 2
    assert claims["communication_style"]["value"] == "新的短句"
    assert "旧的长句" not in json.dumps(document, ensure_ascii=False)


def test_global_persona_generation_keeps_messages_arriving_after_snapshot(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)

    class _DelayedCaller:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003, ANN202
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                content="【沟通风格】：简短\n【人物描述】：会自然聊天的用户"
            )

    caller = _DelayedCaller()
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=caller,
        history_max=2,
        logger=_Logger(),
        profile_service=profiles,
    )

    async def _run() -> None:
        await store.record_message("10001", "snapshot one")
        await store.record_message("10001", "snapshot two")
        await asyncio.wait_for(caller.started.wait(), timeout=1.0)
        await store.record_message("10001", "arrived after snapshot")
        caller.release.set()
        await asyncio.gather(*list(store._tasks))  # noqa: SLF001

    asyncio.run(_run())

    assert store.get_history_count("10001") == 1
    assert store._load_history("10001") == ["arrived after snapshot"]  # noqa: SLF001


def test_persona_clear_all_removes_authoritative_core_and_group_profiles(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="global",
        profile_json={"structured": {"interests": "test"}},
    )
    profiles.upsert_local_profile(
        group_id="20001",
        user_id="10001",
        profile_text="local",
        profile_json=scoped_profile.build_group_profile_document("20001"),
    )
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=object(),
        history_max=5,
        logger=_Logger(),
        profile_service=profiles,
    )

    result = asyncio.run(store.clear_all())

    assert result["core_profiles"] == 1
    assert result["local_profiles"] == 1
    assert profiles.get_core_profile("10001") is None
    assert profiles.get_local_profile(group_id="20001", user_id="10001") is None


def test_persona_clear_fences_cancelled_to_thread_save(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)

    class _ImmediateCaller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003, ANN202
            return SimpleNamespace(content="【沟通风格】：稍后写入")

    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=_ImmediateCaller(),
        history_max=1,
        logger=_Logger(),
        profile_service=profiles,
    )
    original_save = store._save_persona_sync  # noqa: SLF001
    save_started = threading.Event()
    release_save = threading.Event()
    save_finished = threading.Event()

    def _delayed_save(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        save_started.set()
        release_save.wait(timeout=2.0)
        try:
            return original_save(*args, **kwargs)
        finally:
            save_finished.set()

    store._save_persona_sync = _delayed_save  # type: ignore[method-assign]

    async def _run() -> dict[str, int]:
        await store.record_message("10001", "触发画像")
        assert await asyncio.to_thread(save_started.wait, 1.0)
        result = await store.clear_all()
        release_save.set()
        assert await asyncio.to_thread(save_finished.wait, 1.0)
        return result

    result = asyncio.run(_run())

    assert result["cancelled_tasks"] == 1
    assert profiles.get_core_profile("10001") is None
    assert store.get_persona("10001") is None


def test_scoped_refresh_is_fenced_by_profile_clear(tmp_path) -> None:  # noqa: ANN001
    class _DelayedCaller(_Caller):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001, ANN202
            self.started.set()
            await self.release.wait()
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    caller = _DelayedCaller()
    db_path, store, profiles, service, _logger = _runtime(tmp_path, caller=caller)
    _insert(
        db_path,
        user_id="10001",
        content="触发群画像",
        message_id="anchor",
        timestamp=1,
    )

    async def _run():  # noqa: ANN202
        task = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
        )
        await asyncio.wait_for(caller.started.wait(), timeout=1.0)
        await asyncio.to_thread(store.clear_all_profiles)
        caller.release.set()
        return await task

    result = asyncio.run(_run())

    assert result.status == "skipped"
    assert result.code == "profile_generation_changed"
    assert profiles.get_local_profile(group_id="20001", user_id="10001") is None


def test_scoped_model_output_cannot_persist_verbatim_evidence(tmp_path) -> None:  # noqa: ANN001
    caller = _Caller(
        claims_factory=lambda anchors: [
            {
                "key": "recent_focus",
                "value": "RAW_PRIVATE_EVIDENCE_VALUE",
                "confidence": 0.9,
                "evidence_anchor_row_ids": anchors,
            }
        ]
    )
    db_path, _store, profiles, service, _logger = _runtime(tmp_path, caller=caller)
    _insert(
        db_path,
        user_id="10001",
        content="RAW_PRIVATE_EVIDENCE_VALUE",
        message_id="anchor",
        timestamp=1,
    )

    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )

    assert result.status == "succeeded"
    local = profiles.get_local_profile(group_id="20001", user_id="10001")
    assert local is not None
    assert local.profile_json["claims"] == []
    assert "RAW_PRIVATE_EVIDENCE_VALUE" not in json.dumps(local.profile_json)


def test_scoped_generation_rejects_json_wrapped_in_prose(tmp_path) -> None:  # noqa: ANN001
    class _ProseCaller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003, ANN202
            return SimpleNamespace(content='result: {"claims": []}')

    db_path, _store, profiles, service, _logger = _runtime(
        tmp_path,
        caller=_ProseCaller(),
    )
    _insert(
        db_path,
        user_id="10001",
        content="anchor",
        message_id="anchor",
        timestamp=1,
    )

    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )

    assert result.status == "failed"
    assert result.code == "generation_failed"
    assert profiles.get_local_profile(group_id="20001", user_id="10001") is None


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"claims": []}\n```',
        '{"claims":[{"key":"recent_focus","value":"x","confidence":NaN,'
        '"evidence_anchor_row_ids":[1]}]}',
    ],
)
def test_scoped_generation_rejects_non_strict_json(tmp_path, raw: str) -> None:  # noqa: ANN001
    class _RawCaller:
        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003, ANN202
            return SimpleNamespace(content=raw)

    db_path, _store, profiles, service, _logger = _runtime(tmp_path, caller=_RawCaller())
    _insert(
        db_path,
        user_id="10001",
        content="anchor",
        message_id="anchor",
        timestamp=1,
    )

    result = asyncio.run(
        service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
    )

    assert result.status == "failed"
    assert result.code == "generation_failed"
    assert profiles.get_local_profile(group_id="20001", user_id="10001") is None


def test_manual_and_auto_refresh_share_scope_registry(tmp_path) -> None:  # noqa: ANN001
    class _BlockingCaller(_Caller):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001, ANN202
            self.started.set()
            await self.release.wait()
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    caller = _BlockingCaller()
    db_path, _store, _profiles, service, _logger = _runtime(tmp_path, caller=caller)
    _insert(
        db_path,
        user_id="10001",
        content="anchor",
        message_id="anchor",
        timestamp=1,
    )

    async def _run():  # noqa: ANN202
        first = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
        )
        await asyncio.wait_for(caller.started.wait(), timeout=1.0)
        second = await service.refresh_group_profile(
            group_id="20001",
            user_id="10001",
            force=True,
        )
        caller.release.set()
        return await first, second

    first, second = asyncio.run(_run())

    assert first.status == "succeeded"
    assert second.status == "skipped"
    assert second.code == "already_running"
    assert len(caller.prompts) == 1


def test_scoped_refresh_uses_global_generation_semaphore(tmp_path) -> None:  # noqa: ANN001
    class _ConcurrencyCaller(_Caller):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.active = 0
            self.max_active = 0

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001, ANN202
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.entered.set()
            try:
                await self.release.wait()
                return await super().chat_with_tools(messages, tools, use_builtin_search)
            finally:
                self.active -= 1

    caller = _ConcurrencyCaller()
    db_path, _store, _profiles, service, _logger = _runtime(
        tmp_path,
        caller=caller,
        max_concurrency=1,
    )
    _insert(
        db_path,
        user_id="10001",
        content="anchor one",
        message_id="anchor-one",
        timestamp=1,
    )
    _insert(
        db_path,
        user_id="10002",
        content="anchor two",
        message_id="anchor-two",
        timestamp=2,
    )

    async def _run():  # noqa: ANN202
        first = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
        )
        await asyncio.wait_for(caller.entered.wait(), timeout=1.0)
        second = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10002", force=True)
        )
        await asyncio.sleep(0)
        assert caller.active == 1
        caller.release.set()
        return await asyncio.gather(first, second)

    results = asyncio.run(_run())

    assert [result.status for result in results] == ["succeeded", "succeeded"]
    assert caller.max_active == 1


def test_close_waits_for_inflight_atomic_write_worker(tmp_path) -> None:  # noqa: ANN001
    db_path, store, profiles, service, _logger = _runtime(tmp_path)
    _insert(
        db_path,
        user_id="10001",
        content="anchor",
        message_id="anchor",
        timestamp=1,
    )
    original_write = store.atomic_patch_local_profile
    write_started = threading.Event()
    release_write = threading.Event()

    def _delayed_write(**kwargs):  # noqa: ANN003, ANN202
        write_started.set()
        release_write.wait(timeout=2.0)
        return original_write(**kwargs)

    store.atomic_patch_local_profile = _delayed_write  # type: ignore[method-assign]

    async def _run() -> None:
        refresh = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
        )
        assert await asyncio.to_thread(write_started.wait, 1.0)
        closing = asyncio.create_task(service.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release_write.set()
        await asyncio.wait_for(closing, timeout=1.0)
        assert refresh.cancelled()

    asyncio.run(_run())

    saved = profiles.get_local_profile(group_id="20001", user_id="10001")
    assert saved is not None
    revision = saved.profile_json["revision"]
    threading.Event().wait(0.05)
    assert profiles.get_local_profile(
        group_id="20001",
        user_id="10001",
    ).profile_json["revision"] == revision


def test_persona_store_honors_dynamic_disable_and_reenable(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    profiles.upsert_core_profile(
        user_id="10001",
        profile_text="persisted persona",
        profile_json={},
    )
    state = {"enabled": False}
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=object(),
        history_max=5,
        logger=_Logger(),
        profile_service=profiles,
        enabled_getter=lambda: state["enabled"],
    )

    assert store.get_persona("10001") is None
    asyncio.run(store.record_message("10001", "disabled message"))
    assert store.get_history_count("10001") == 0
    assert asyncio.run(store.apply_user_correction("10001", {"职业": "设计师"})) is None

    state["enabled"] = True
    assert store.get_persona_text("10001") == "persisted persona"
    state["enabled"] = False
    assert store.get_persona_snippet("10001") == ""


def test_persona_generation_started_before_hot_disable_cannot_commit(tmp_path) -> None:  # noqa: ANN001
    _db_path, _store, profiles, _service, _logger = _runtime(tmp_path)
    state = {"enabled": True}

    class _DelayedCaller:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def chat_with_tools(self, **_kwargs):  # noqa: ANN003, ANN202
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(content="【沟通风格】：不应写入")

    caller = _DelayedCaller()
    store = persona_service.PersonaStore(
        data_dir=tmp_path,
        tool_caller=caller,
        history_max=1,
        logger=_Logger(),
        profile_service=profiles,
        enabled_getter=lambda: state["enabled"],
    )

    async def _run() -> None:
        await store.record_message("10001", "trigger")
        await asyncio.wait_for(caller.started.wait(), timeout=1.0)
        state["enabled"] = False
        caller.release.set()
        await asyncio.gather(*list(store._tasks))  # noqa: SLF001

    asyncio.run(_run())

    assert profiles.get_core_profile("10001") is None
    assert store.get_history_count("10001") == 1


def test_dirty_scoped_refresh_before_clear_does_not_restart_after_clear(tmp_path) -> None:  # noqa: ANN001
    class _DelayedCaller(_Caller):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def chat_with_tools(self, messages, tools, use_builtin_search):  # noqa: ANN001, ANN202
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return await super().chat_with_tools(messages, tools, use_builtin_search)

    caller = _DelayedCaller()
    db_path, store, profiles, service, _logger = _runtime(tmp_path, caller=caller)
    _insert(
        db_path,
        user_id="10001",
        content="first",
        message_id="first",
        timestamp=1,
    )

    async def _run():  # noqa: ANN202
        refresh = asyncio.create_task(
            service.refresh_group_profile(group_id="20001", user_id="10001", force=True)
        )
        await asyncio.wait_for(caller.started.wait(), timeout=1.0)
        service.observe_group_message("20001", "10001")
        await asyncio.to_thread(store.clear_all_profiles)
        caller.release.set()
        result = await refresh
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(_run())

    assert result.code == "profile_generation_changed"
    assert caller.calls == 1
    assert profiles.get_local_profile(group_id="20001", user_id="10001") is None


def test_scoped_profile_runtime_wiring_uses_record_matcher_and_dynamic_switch() -> None:
    root = Path(__file__).resolve().parents[1]
    composition_source = (root / "handlers" / "setup" / "composition.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(composition_source)
    call_keywords: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name in {"register_reply_matchers", "register_chat_matchers"}:
            call_keywords[name] = {keyword.arg or "" for keyword in node.keywords}
    assert "create_scoped_profile_task" not in call_keywords["register_reply_matchers"]
    assert "create_scoped_profile_task" in call_keywords["register_chat_matchers"]

    runtime_source = (root / "core" / "runtime_builder.py").read_text(encoding="utf-8")
    hook_source = (root / "core" / "builtin_hooks.py").read_text(encoding="utf-8")
    assert "enabled_getter=persona_enabled" in runtime_source
    assert 'if getattr(plugin_config, "personification_persona_enabled", True):' not in runtime_source
    assert 'getattr(ctx.plugin_config, "personification_persona_enabled", True)' in hook_source
