from __future__ import annotations

import io
import json
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

from ._loader import load_personification_module


@pytest.fixture
def transfer(tmp_path: Path):
    db = load_personification_module("plugin.personification.core.db")
    service_mod = load_personification_module("plugin.personification.core.data_transfer.service")
    db_path = db.init_db_sync(tmp_path)
    return service_mod.DataTransferService(data_dir=tmp_path / "transfer", db_path=db_path), db_path


def _seed(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO group_messages(group_id,user_id,content,timestamp) VALUES('g1','u1','one',1)")
        conn.execute("INSERT INTO group_messages(group_id,user_id,content,timestamp) VALUES('g2','u2','secret-other-group',2)")
        conn.execute("INSERT INTO session_messages(session_id,role,content,timestamp) VALUES('group_g1','user','session-one',1)")
        conn.execute("INSERT INTO session_messages(session_id,role,content,timestamp) VALUES('private_u1','user','private-secret',1)")
        conn.execute("INSERT INTO conversation_threads VALUES('g1:t1','g1','topic','[]',1,2)")
        conn.execute("INSERT INTO group_relation_edges VALUES('g1','u1','u2','reply',1,2,'m1')")
        conn.execute("INSERT INTO group_style_snapshots(group_id,style_text,created_at) VALUES('g1','style',1)")
        conn.execute("INSERT INTO kv_store VALUES('group_config','__root__',?,1)", (json.dumps({"g1": {"enabled": True, "custom_prompt": "safe", "api_key": "never"}, "g2": {"custom_prompt": "other"}}),))
        conn.execute("INSERT INTO kv_store VALUES('webui_devices','__root__','{\"token\":{}}',1)")
        conn.execute("INSERT INTO kv_store VALUES('proactive_state','__root__','{\"g1\":{\"active\":true}}',1)")
        conn.commit()


def _archive_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _apply_from_plan(service, task_id: str, *, bot_id: str = "bot1", group_id: str = "g1", mode: str = "merge"):
    plan = service.dry_run(task_id, target_bot_id=bot_id, target_group_id=group_id, mode=mode)
    return service.apply(task_id, target_bot_id=bot_id, target_group_id=group_id, mode=mode, plan_token=plan["plan_token"])


def test_group_safe_export_scope_and_secret_exclusion(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    task = service.create_export(bot_id="bot1", group_id="g1")
    path = service.export_path(task["task_id"])
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert all("sqlite" not in name for name in names)
        combined = b"".join(archive.read(name) for name in names if name.startswith("datasets/"))
        assert b"secret-other-group" not in combined
        assert b"private-secret" not in combined
        assert b"token" not in combined
        assert b"api_key" not in combined
        manifest = json.loads(archive.read("manifest.json"))
        assert "group_messages" not in manifest["datasets"]
        assert manifest["scope"] == {"kind": "group", "group_id": "g1"}
        assert manifest["source"] == {"bot_id": "bot1", "group_id": "g1"}
        assert "auth" in manifest["excluded"] and "qzone" in manifest["excluded"]
        assert manifest["version"] == 3
        assert "user_policy" in manifest["excluded"]


def test_v1_package_remains_import_compatible(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    exported = service.create_export(
        bot_id="bot1",
        group_id="g1",
        datasets=["group_messages", "group_relation_edges"],
    )
    rewritten = io.BytesIO()
    with zipfile.ZipFile(service.export_path(exported["task_id"])) as source:
        with zipfile.ZipFile(rewritten, "w") as target:
            for name in source.namelist():
                payload = source.read(name)
                if name == "manifest.json":
                    manifest = json.loads(payload)
                    manifest["version"] = 1
                    payload = json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                target.writestr(name, payload)
    uploaded = service.store_upload(io.BytesIO(rewritten.getvalue()))

    inspected = service.inspect(uploaded["task_id"])

    assert inspected["valid"] is True
    assert inspected["manifest"]["version"] == 1
    assert inspected["counts"] == {
        "group_messages": 1,
        "group_relation_edges": 1,
    }


def test_v2_avatar_relation_evidence_validation_is_fail_closed(transfer) -> None:
    service, _db_path = transfer
    error_type = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    ).DataTransferError
    manifest = {"scope": {"group_id": "20001"}}
    valid = {
        "group_id": "20001",
        "left_user_id": "10001",
        "right_user_id": "10002",
        "relation": "coordinated_pair",
        "confidence": 0.9,
        "evidence_tags": '["matching_palette"]',
        "asset_kinds": '["illustration"]',
        "schema_version": 1,
        "observed_at": 100.0,
        "expires_at": 200.0,
    }
    service._validate_values(manifest, {"avatar_relation_evidence": [valid]})
    invalid_updates = (
        {"relation": "real_world_couple"},
        {"confidence": float("nan")},
        {"evidence_tags": '["matching_palette","injected_claim"]'},
        {"evidence_tags": "[{}]"},
        {"asset_kinds": "not-json"},
        {"schema_version": 2},
        {"left_user_id": "10002", "right_user_id": "10001"},
        {"expires_at": 50.0},
    )
    for update in invalid_updates:
        with pytest.raises(error_type):
            service._validate_values(
                manifest,
                {"avatar_relation_evidence": [{**valid, **update}]},
            )


def test_v2_manifest_cannot_claim_v3_meme_dictionary_dataset(transfer) -> None:
    service, _db_path = transfer
    constants = load_personification_module("plugin.personification.core.data_transfer.constants")
    error_type = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    ).DataTransferError
    manifest = {
        "format": constants.FORMAT,
        "version": 2,
        "source": {"bot_id": "bot1", "group_id": "g1"},
        "scope": {"kind": "group", "group_id": "g1"},
        "datasets": ["meme_dictionary"],
        "files": {"datasets/meme_dictionary.json": {"sha256": "0" * 64, "size": 0}},
    }

    with pytest.raises(error_type, match="invalid datasets"):
        service._validate_manifest(manifest)


def test_v3_meme_dictionary_transfer_excludes_login_and_author_fingerprints(transfer, tmp_path: Path) -> None:
    service, db_path = transfer
    profile = tmp_path / "mcp" / "social_platform" / "bilibili" / "profile"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("private-login-cookie", encoding="utf-8")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO meme_dictionary(
                   term,aliases,meaning,tone,risk_level,examples,scope,group_id,
                   confidence,evidence_message_ids,safe_usage,managed_by,updated_at
               ) VALUES('刘涛','[]','六级防具','[]','low','[]','public','',0.95,'[]',
                        '仅在三角洲行动语境中使用','auto',1000)"""
        )
        conn.execute(
            """INSERT INTO meme_senses(
                   sense_id,scope,group_id,term,meaning,aliases_json,game_context_json,
                   version_context,usage_context,safe_usage,risk_level,status,confidence,
                   source_count,platform_count,auto_managed,first_seen_at,last_verified_at,
                   reverify_after,expires_at,manual_locked,revision,updated_at
               ) VALUES(
                   'sense-public','public','','刘涛','六级防具','[]',
                   '{"canonical_name":"三角洲行动","aliases":["三角洲"]}',
                   '','讨论护甲时','仅在三角洲行动语境中使用','low','verified',0.95,
                   3,3,1,1000,1000,2000,3000,0,4,1000
               )"""
        )
        conn.execute(
            """INSERT INTO meme_evidence_claims(
                   claim_id,sense_id,packet_id,platform,content_id,canonical_url,content_type,
                   discussion_id,evidence_type,author_fingerprint,quote,content_fingerprint,
                   media_fingerprint,source_cluster_id,extractor_version,model_route,confidence,
                   source_quality,published_at,retrieved_at,created_at
               ) VALUES(
                   'claim-public','sense-public','packet-public','bilibili','BV1',
                   'https://www.bilibili.com/video/BV1?token=private-login-token#reply',
                   'video','reply-1','comment','private-author-fingerprint','刘涛就是六级甲',
                   'content-fp','media-fp','source-public','extractor-v1','social-route',
                   0.93,0.9,900,1000,1000
               )"""
        )
        conn.execute(
            """INSERT INTO meme_learning_events(
                   event_id,sense_id,event_type,old_status,new_status,actor,detail_json,created_at
               ) VALUES(
                   'event-public','sense-public','status_changed','understand_only','verified',
                   'system','{"source_count":3,"cookie":"must-not-transfer"}',1000
               )"""
        )
        conn.execute(
            """INSERT INTO meme_senses(
                   sense_id,scope,group_id,term,meaning,first_seen_at,updated_at
               ) VALUES('sense-other','group','g2','别群词','不应导出',1000,1000)"""
        )
        conn.commit()

    exported = service.create_export(
        bot_id="bot1",
        group_id="g1",
        datasets=["meme_dictionary"],
    )
    package = service.export_path(exported["task_id"])
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        payload_bytes = archive.read("datasets/meme_dictionary.json")
        payload = json.loads(payload_bytes)

    assert manifest["version"] == 3
    assert manifest["datasets"] == ["meme_dictionary"]
    rendered = payload_bytes.decode("utf-8")
    assert "刘涛" in rendered
    assert "别群词" not in rendered
    assert "author_fingerprint" not in rendered
    assert "private-author-fingerprint" not in rendered
    assert "private-login-token" not in rendered
    assert "private-login-cookie" not in rendered
    assert payload["evidence"][0]["canonical_url"] == "https://www.bilibili.com/video/BV1"
    assert payload["events"][0]["detail_json"] == "{}"

    poisoned = json.loads(json.dumps(payload, ensure_ascii=False))
    poisoned["events"][0]["detail_json"] = '{"loginToken":"must-not-transfer"}'
    error_type = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    ).DataTransferError
    with pytest.raises(error_type, match="forbidden secret"):
        service._validate_values(
            {"scope": {"group_id": "g1"}},
            {"meme_dictionary": poisoned},
        )

    duplicate_source = json.loads(json.dumps(payload, ensure_ascii=False))
    duplicate_source["evidence"].append({
        **duplicate_source["evidence"][0],
        "claim_id": "claim-duplicate-source",
    })
    with pytest.raises(error_type, match="duplicate meme evidence source"):
        service._validate_values(
            {"scope": {"group_id": "g1"}},
            {"meme_dictionary": duplicate_source},
        )

    uploaded = service.store_upload(io.BytesIO(package.read_bytes()))
    inspected = service.inspect(uploaded["task_id"])
    assert inspected["valid"] is True
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM meme_learning_events WHERE sense_id='sense-public'")
        conn.execute("DELETE FROM meme_evidence_claims WHERE sense_id='sense-public'")
        conn.execute("DELETE FROM meme_senses WHERE sense_id='sense-public'")
        conn.execute("DELETE FROM meme_dictionary WHERE term='刘涛' AND scope='public'")
        conn.commit()

    applied = _apply_from_plan(service, uploaded["task_id"])
    with sqlite3.connect(db_path) as conn:
        restored = conn.execute(
            "SELECT canonical_url,author_fingerprint FROM meme_evidence_claims WHERE claim_id='claim-public'"
        ).fetchone()
        assert restored == ("https://www.bilibili.com/video/BV1", "")
        assert conn.execute("SELECT status FROM meme_senses WHERE sense_id='sense-public'").fetchone()[0] == "verified"
        assert conn.execute("SELECT COUNT(*) FROM meme_learning_events WHERE event_id='event-public'").fetchone()[0] == 1

    service.rollback(applied["journal_id"])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM meme_senses WHERE sense_id='sense-public'").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM meme_dictionary WHERE term='刘涛' AND scope='public'").fetchone()[0] == 0


def test_meme_transfer_global_ids_cannot_cross_scope_or_sense(transfer) -> None:
    service, db_path = transfer
    error_type = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    ).DataTransferError
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """INSERT INTO meme_senses(
                   sense_id,scope,group_id,term,meaning,first_seen_at,updated_at
               ) VALUES('shared-sense','group','g2','别群词','别群含义',1,1)"""
        )
        conn.execute(
            """INSERT INTO meme_evidence_claims(
                   claim_id,sense_id,platform,content_id,quote,source_cluster_id,created_at
               ) VALUES('shared-claim','shared-sense','bilibili','BV1','明确解释','cluster-1',1)"""
        )
        conn.execute(
            """INSERT INTO meme_learning_events(
                   event_id,sense_id,event_type,created_at
               ) VALUES('shared-event','shared-sense','observed',1)"""
        )
        conn.commit()

        with pytest.raises(error_type, match="sense id conflicts with another scope"):
            service._check_meme_dictionary_conflicts(conn, {
                "senses": [{"sense_id": "shared-sense", "scope": "group", "group_id": "g1"}],
                "evidence": [],
                "events": [],
            })
        with pytest.raises(error_type, match="evidence id conflicts with another sense"):
            service._check_meme_dictionary_conflicts(conn, {
                "senses": [],
                "evidence": [{"claim_id": "shared-claim", "sense_id": "different-sense"}],
                "events": [],
            })
        with pytest.raises(error_type, match="events id conflicts with another sense"):
            service._check_meme_dictionary_conflicts(conn, {
                "senses": [],
                "evidence": [],
                "events": [{"event_id": "shared-event", "sense_id": "different-sense"}],
            })


def test_inspect_rejects_zip_slip_duplicate_and_undeclared(transfer, tmp_path: Path) -> None:
    service, _ = transfer
    error_type = load_personification_module("plugin.personification.core.data_transfer.service").DataTransferError
    for index, builder in enumerate((
        lambda z: z.writestr("../escape", b"x"),
        lambda z: (z.writestr("same", b"1"), z.writestr("same", b"2")),
        lambda z: z.writestr("undeclared", b"x"),
        lambda z: (z.writestr("A.json", b"1"), z.writestr("a.json", b"2")),
    )):
        path = tmp_path / f"bad{index}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", b"{}")
            builder(archive)
        task = service.store_upload(io.BytesIO(path.read_bytes()))
        with pytest.raises(error_type):
            service.inspect(task["task_id"])


def test_dry_run_identity_apply_idempotency_replace_and_rollback(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    constants = load_personification_module("plugin.personification.core.data_transfer.constants")
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=constants.DATASETS)
    uploaded = service.store_upload(io.BytesIO(_archive_bytes(service.export_path(exported["task_id"]))))
    with pytest.raises(Exception, match="cross-bot"):
        service.dry_run(uploaded["task_id"], target_bot_id="bot2", target_group_id="g1")
    plan = service.dry_run(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace")
    assert plan["activations"] == []
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE group_messages SET content='changed' WHERE group_id='g1'")
        conn.commit()
    first = service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace", plan_token=plan["plan_token"])
    second = service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace", plan_token=plan["plan_token"])
    assert second["idempotent"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT content FROM group_messages WHERE group_id='g1'").fetchone()[0] == "one"
        assert conn.execute("SELECT content FROM group_messages WHERE group_id='g2'").fetchone()[0] == "secret-other-group"
        conn.execute("INSERT INTO group_messages(group_id,user_id,content,timestamp) VALUES('g2','u3','created-after-import',3)")
        conn.commit()
    rolled = service.rollback(first["journal_id"])
    assert rolled["success"] is True
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT content FROM group_messages WHERE group_id='g1'").fetchone()[0] == "changed"
        assert conn.execute("SELECT content FROM group_messages WHERE content='created-after-import'").fetchone()[0] == "created-after-import"


def test_apply_requires_matching_unmodified_plan_token(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    exported = service.create_export(bot_id="bot1", group_id="g1")
    uploaded = service.store_upload(io.BytesIO(_archive_bytes(service.export_path(exported["task_id"]))))
    plan = service.dry_run(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="merge")
    with pytest.raises(Exception, match="plan token"):
        service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace", plan_token=plan["plan_token"])
    forged = plan["plan_token"][:-1] + ("A" if plan["plan_token"][-1] != "A" else "B")
    with pytest.raises(Exception, match="plan token"):
        service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", plan_token=forged)


def test_apply_failure_auto_rolls_back_and_leaves_terminal_journal(transfer, monkeypatch) -> None:
    service, db_path = transfer
    _seed(db_path)
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=["group_messages"])
    uploaded = service.store_upload(io.BytesIO(_archive_bytes(service.export_path(exported["task_id"]))))
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE group_messages SET content='before-failure' WHERE group_id='g1'")
        conn.commit()
    plan = service.dry_run(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace")

    def fail_memory(*_args, **_kwargs):
        raise RuntimeError("injected memory failure")

    monkeypatch.setattr(service, "_apply_memory_scope", fail_memory)
    with pytest.raises(RuntimeError, match="injected"):
        service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", mode="scope-replace", plan_token=plan["plan_token"])
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT content FROM group_messages WHERE group_id='g1'").fetchone()[0] == "before-failure"
        assert conn.execute("SELECT content FROM group_messages WHERE group_id='g2'").fetchone()[0] == "secret-other-group"
        status = conn.execute("SELECT status FROM data_transfer_journal ORDER BY created_at DESC LIMIT 1").fetchone()[0]
    assert status == "rolled_back"


def test_merge_existing_autoincrement_id_and_rollback_use_same_key(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=["group_messages"])
    uploaded = service.store_upload(io.BytesIO(_archive_bytes(service.export_path(exported["task_id"]))))
    with sqlite3.connect(db_path) as conn:
        original_id = conn.execute("SELECT id FROM group_messages WHERE group_id='g1'").fetchone()[0]
        conn.execute("UPDATE group_messages SET content='target-before-merge' WHERE id=?", (original_id,))
        conn.commit()

    applied = _apply_from_plan(service, uploaded["task_id"], mode="merge")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id,content FROM group_messages WHERE id=?", (original_id,)).fetchall()
    assert rows == [(original_id, "one")]

    service.rollback(applied["journal_id"])
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT id,content FROM group_messages WHERE id=?", (original_id,)).fetchall()
    assert rows == [(original_id, "target-before-merge")]


def test_main_snapshot_and_apply_hold_one_immediate_transaction(transfer, monkeypatch) -> None:
    service, db_path = transfer
    _seed(db_path)
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=["group_messages"])
    uploaded = service.store_upload(io.BytesIO(_archive_bytes(service.export_path(exported["task_id"]))))
    original = service._write_scope_snapshot
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer: list[threading.Thread] = []

    def concurrent_write() -> None:
        writer_started.set()
        with sqlite3.connect(db_path, timeout=5) as conn:
            conn.execute("INSERT INTO group_messages(group_id,user_id,content,timestamp) VALUES('g2','u9','concurrent-main',9)")
            conn.commit()
        writer_done.set()

    def observed_snapshot(*args, **kwargs):
        thread = threading.Thread(target=concurrent_write)
        writer.append(thread)
        thread.start()
        assert writer_started.wait(1)
        time.sleep(0.05)
        assert not writer_done.is_set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_write_scope_snapshot", observed_snapshot)
    _apply_from_plan(service, uploaded["task_id"], mode="merge")
    writer[0].join(2)
    assert writer_done.is_set()


def test_checksum_and_cross_group_rows_rejected(transfer) -> None:
    service, db_path = transfer
    _seed(db_path)
    constants = load_personification_module("plugin.personification.core.data_transfer.constants")
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=constants.DATASETS)
    source = service.export_path(exported["task_id"])
    broken = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(broken, "w") as target:
        for name in original.namelist():
            data = original.read(name)
            if name.endswith("group_messages.json"):
                rows = json.loads(data)
                rows[0]["group_id"] = "g2"
                data = json.dumps(rows).encode()
            target.writestr(name, data)
    uploaded = service.store_upload(io.BytesIO(broken.getvalue()))
    with pytest.raises(Exception, match="checksum mismatch"):
        service.inspect(uploaded["task_id"])


def test_webui_routes_require_admin(transfer) -> None:
    _, db_path = transfer
    route_mod = load_personification_module("plugin.personification.webui.routes.data_transfer_routes")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from types import SimpleNamespace

    runtime = SimpleNamespace(plugin_config=SimpleNamespace(personification_data_dir=str(db_path.parent)))
    app = FastAPI()
    app.include_router(route_mod.build_data_transfer_router(runtime=runtime))
    client = TestClient(app)
    assert client.post("/api/data-transfer/exports/create", json={"bot_id": "b", "group_id": "g"}).status_code == 401
    assert client.get("/api/data-transfer/exports/unknown/status").status_code == 401
    assert client.get("/api/data-transfer/imports/unknown/inspect").status_code == 401
    assert client.post("/api/data-transfer/imports/unknown/rollback").status_code == 401


def test_group_profiles_and_memories_round_trip_with_rollback(tmp_path: Path) -> None:
    db = load_personification_module("plugin.personification.core.db")
    memory_mod = load_personification_module("plugin.personification.core.memory_store")
    service_mod = load_personification_module("plugin.personification.core.data_transfer.service")
    db_path = db.init_db_sync(tmp_path / "main")
    cfg = type("Cfg", (), {
        "personification_data_dir": str(tmp_path / "runtime"),
        "personification_memory_enabled": True,
        "personification_memory_palace_enabled": True,
    })()
    store = memory_mod.MemoryStore(cfg)
    store.initialize()
    store.upsert_local_profile(
        group_id="g1",
        user_id="u1",
        profile_text="本群画像",
        profile_json={"qq_profile": {"nickname": "小明", "email": "must-not-export@example.com"}},
    )
    store.write_memory_item({
        "memory_id": "m1",
        "memory_type": "group_knowledge",
        "palace_zone": "group",
        "summary": "这个群最近在做迁移测试",
        "group_id": "g1",
    })
    service = service_mod.DataTransferService(
        data_dir=tmp_path / "transfer",
        db_path=db_path,
        memory_store=store,
    )
    exported = service.create_export(bot_id="bot1", group_id="g1")
    package = service.export_path(exported["task_id"])
    with zipfile.ZipFile(package) as archive:
        payload = b"".join(archive.read(name) for name in archive.namelist() if name.startswith("datasets/"))
        assert "本群画像".encode() in payload
        assert "这个群最近在做迁移测试".encode() in payload
        assert b"must-not-export@example.com" not in payload

    store.upsert_local_profile(group_id="g1", user_id="u1", profile_text="目标端修改", profile_json={})
    store.write_memory_item({"memory_id": "m1", "memory_type": "group_knowledge", "palace_zone": "group", "summary": "目标端修改", "group_id": "g1"})
    uploaded = service.store_upload(io.BytesIO(package.read_bytes()))
    applied = _apply_from_plan(service, uploaded["task_id"], mode="scope-replace")
    assert store.get_local_profile(group_id="g1", user_id="u1")["profile_text"] == "本群画像"
    assert store.get_memory_item("m1")["summary"] == "这个群最近在做迁移测试"
    service.rollback(applied["journal_id"])
    assert store.get_local_profile(group_id="g1", user_id="u1")["profile_text"] == "目标端修改"
    assert store.get_memory_item("m1")["summary"] == "目标端修改"


def test_v3_filters_blocked_users_and_transfers_visual_evidence_without_hashes(
    tmp_path: Path,
) -> None:
    db_mod = load_personification_module("plugin.personification.core.db")
    memory_mod = load_personification_module(
        "plugin.personification.core.memory_store"
    )
    policy_mod = load_personification_module(
        "plugin.personification.core.user_policy"
    )
    service_mod = load_personification_module(
        "plugin.personification.core.data_transfer.service"
    )
    db_path = db_mod.init_db_sync(tmp_path / "main")
    cfg = type(
        "Cfg",
        (),
        {
            "personification_data_dir": str(tmp_path / "runtime"),
            "personification_memory_enabled": True,
            "personification_memory_palace_enabled": True,
        },
    )()
    store = memory_mod.MemoryStore(cfg)
    store.initialize()
    policy = policy_mod.UserPolicyService(
        db_path=db_path,
        evidence_key=b"p" * 32,
    )
    blocked_state = policy.set_manual_override(
        user_id="10002",
        mode="block",
        actor="test",
    )
    assert blocked_state.is_blocked()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO group_messages(group_id,user_id,nickname,content,timestamp) VALUES('20001','10001','甲','allowed-one',1)"
        )
        conn.execute(
            "INSERT INTO group_messages(group_id,user_id,nickname,content,timestamp) VALUES('20001','10002','乙','blocked-secret',2)"
        )
        conn.execute(
            "INSERT INTO group_messages(group_id,user_id,nickname,content,timestamp) VALUES('20001','10003','丙','allowed-two',3)"
        )
        conn.execute(
            "INSERT INTO group_relation_edges VALUES('20001','10001','10002','reply',1,2,'m1')"
        )
        conn.execute(
            "INSERT INTO group_relation_edges VALUES('20001','10001','10003','reply',1,3,'m2')"
        )
        for left, right, observed in (
            ("10001", "10002", 2),
            ("10001", "10003", 3),
        ):
            conn.execute(
                "INSERT INTO avatar_relation_evidence(group_id,left_user_id,right_user_id,relation,confidence,evidence_tags,asset_kinds,left_avatar_hash,right_avatar_hash,schema_version,observed_at,expires_at) VALUES('20001',?,?, 'coordinated_pair',0.9,'[]','[]',?,?,1,?,9999999999)",
                (left, right, left[-1] * 64, right[-1] * 64, observed),
            )
        conn.commit()
    for user_id in ("10001", "10002", "10003"):
        store.upsert_local_profile(
            group_id="20001",
            user_id=user_id,
            profile_text=f"profile-{user_id}",
        )
        store.write_memory_item(
            {
                "memory_id": f"memory-{user_id}",
                "summary": f"memory summary {user_id}",
                "group_id": "20001",
                "user_id": user_id,
            }
        )

    source = service_mod.DataTransferService(
        data_dir=tmp_path / "source-transfer",
        db_path=db_path,
        memory_store=store,
    )
    datasets = [
        "group_messages",
        "group_relation_edges",
        "avatar_relation_evidence",
        "local_user_profiles",
        "group_memories",
    ]
    exported = source.create_export(
        bot_id="bot1",
        group_id="20001",
        datasets=datasets,
    )
    package = source.export_path(exported["task_id"])
    with zipfile.ZipFile(package) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        visual_rows = json.loads(
            archive.read("datasets/avatar_relation_evidence.json")
        )
        combined = b"".join(
            archive.read(name)
            for name in archive.namelist()
            if name.startswith("datasets/")
        )
    assert manifest["version"] == 3
    assert len(visual_rows) == 2
    assert all("avatar_hash" not in key for row in visual_rows for key in row)
    assert b"user_policy_state" not in combined

    target = service_mod.DataTransferService(
        data_dir=tmp_path / "target-transfer",
        db_path=db_path,
        memory_store=store,
        user_policy_service=policy,
    )
    uploaded = target.store_upload(io.BytesIO(package.read_bytes()))
    inspected = target.inspect(uploaded["task_id"])
    assert inspected["counts"] == {
        "group_messages": 2,
        "group_relation_edges": 1,
        "avatar_relation_evidence": 1,
        "local_user_profiles": 2,
        "group_memories": 2,
    }

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE group_messages SET content='frozen-blocked' WHERE group_id='20001' AND user_id='10002'"
        )
        conn.commit()
    store.upsert_local_profile(
        group_id="20001",
        user_id="10002",
        profile_text="frozen-blocked-profile",
    )
    store.write_memory_item(
        {
            "memory_id": "memory-10002",
            "summary": "frozen blocked memory",
            "group_id": "20001",
            "user_id": "10002",
            "revision": 50,
        }
    )

    plan = target.dry_run(
        uploaded["task_id"],
        target_bot_id="bot1",
        target_group_id="20001",
        mode="scope-replace",
    )
    target.apply(
        uploaded["task_id"],
        target_bot_id="bot1",
        target_group_id="20001",
        mode="scope-replace",
        plan_token=plan["plan_token"],
    )

    with sqlite3.connect(db_path) as conn:
        blocked_message = conn.execute(
            "SELECT content FROM group_messages WHERE group_id='20001' AND user_id='10002'"
        ).fetchone()[0]
        blocked_edge_count = conn.execute(
            "SELECT COUNT(*) FROM group_relation_edges WHERE group_id='20001' AND (src_user_id='10002' OR dst_user_id='10002')"
        ).fetchone()[0]
        blocked_visual_count = conn.execute(
            "SELECT COUNT(*) FROM avatar_relation_evidence WHERE group_id='20001' AND (left_user_id='10002' OR right_user_id='10002')"
        ).fetchone()[0]
    assert blocked_message == "frozen-blocked"
    assert blocked_edge_count == 1
    assert blocked_visual_count == 1
    assert store.get_local_profile(
        group_id="20001",
        user_id="10002",
    )["profile_text"] == "frozen-blocked-profile"
    assert store.get_memory_item("memory-10002")["summary"] == "frozen blocked memory"


def test_memory_id_owned_by_another_group_is_rejected(tmp_path: Path) -> None:
    db = load_personification_module("plugin.personification.core.db")
    memory_mod = load_personification_module("plugin.personification.core.memory_store")
    service_mod = load_personification_module("plugin.personification.core.data_transfer.service")
    db_path = db.init_db_sync(tmp_path / "main")
    cfg = type("Cfg", (), {"personification_data_dir": str(tmp_path / "runtime"), "personification_memory_enabled": True, "personification_memory_palace_enabled": True})()
    store = memory_mod.MemoryStore(cfg)
    store.initialize()
    store.write_memory_item({"memory_id": "shared-id", "summary": "source", "group_id": "g1"})
    source_service = service_mod.DataTransferService(data_dir=tmp_path / "source-transfer", db_path=db_path, memory_store=store)
    exported = source_service.create_export(bot_id="bot1", group_id="g1", datasets=["group_memories"])
    store.write_memory_item({"memory_id": "shared-id", "summary": "other group", "group_id": "g2", "revision": 99})
    uploaded = source_service.store_upload(io.BytesIO(source_service.export_path(exported["task_id"]).read_bytes()))
    plan = source_service.dry_run(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1")
    with pytest.raises(Exception, match="another group"):
        source_service.apply(uploaded["task_id"], target_bot_id="bot1", target_group_id="g1", plan_token=plan["plan_token"])


def test_migration_and_normal_memory_profile_writes_share_maintenance_lock(tmp_path: Path, monkeypatch) -> None:
    db = load_personification_module("plugin.personification.core.db")
    memory_mod = load_personification_module("plugin.personification.core.memory_store")
    service_mod = load_personification_module("plugin.personification.core.data_transfer.service")
    db_path = db.init_db_sync(tmp_path / "main")
    cfg = type("Cfg", (), {"personification_data_dir": str(tmp_path / "runtime"), "personification_memory_enabled": True, "personification_memory_palace_enabled": True})()
    store = memory_mod.MemoryStore(cfg)
    store.initialize()
    store.write_memory_item({"memory_id": "m1", "summary": "package", "group_id": "g1"})
    service = service_mod.DataTransferService(data_dir=tmp_path / "transfer", db_path=db_path, memory_store=store)
    exported = service.create_export(bot_id="bot1", group_id="g1", datasets=["group_memories"])
    uploaded = service.store_upload(io.BytesIO(service.export_path(exported["task_id"]).read_bytes()))
    original = service._apply_memory_scope
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer: list[threading.Thread] = []

    def concurrent_write() -> None:
        writer_started.set()
        store.write_memory_item({"memory_id": "m2", "summary": "normal concurrent write", "group_id": "g1"})
        store.upsert_local_profile(group_id="g1", user_id="u2", profile_text="normal profile write", profile_json={})
        writer_done.set()

    def observed_apply(*args, **kwargs):
        thread = threading.Thread(target=concurrent_write)
        writer.append(thread)
        thread.start()
        assert writer_started.wait(1)
        time.sleep(0.05)
        assert not writer_done.is_set()
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "_apply_memory_scope", observed_apply)
    _apply_from_plan(service, uploaded["task_id"])
    writer[0].join(2)
    assert writer_done.is_set()
    assert store.get_memory_item("m2")["summary"] == "normal concurrent write"
    assert store.get_local_profile(group_id="g1", user_id="u2")["profile_text"] == "normal profile write"


def test_data_transfer_success_diagnostics_preserve_operation_fields() -> None:
    route_mod = load_personification_module("plugin.personification.webui.routes.data_transfer_routes")
    cases = {
        "create": {"task_id": "create-task", "status": "completed", "metadata": {"manifest": {"datasets": ["group_state"]}}},
        "upload": {"task_id": "upload-task", "status": "uploaded", "metadata": {"size": 123}},
        "inspect": {"valid": True, "manifest": {"package_id": "package"}, "counts": {"group_state": 1}},
        "dry-run": {"valid": True, "mode": "merge", "changes": {"group_state": 1}, "plan_token": "safe-plan-token"},
        "apply": {"success": True, "journal_id": "journal", "idempotent": False},
        "rollback": {"success": True, "journal_id": "journal", "idempotent": False},
    }
    required = {"ok", "code", "phase", "title", "message", "details", "steps", "retryable", "partial", "outcome_unknown"}
    for operation, original in cases.items():
        payload = route_mod._success_payload(operation, original, mode="merge")
        assert required <= payload.keys()
        assert payload["ok"] is True
        assert payload["phase"] == operation
        assert payload["steps"]
        for key, value in original.items():
            assert payload[key] == value


def test_data_transfer_error_diagnostics_use_safe_stable_codes() -> None:
    route_mod = load_personification_module("plugin.personification.webui.routes.data_transfer_routes")
    error_type = route_mod.DataTransferError
    invalid = route_mod._data_transfer_error_diagnostic(
        error_type(r"invalid package: C:\private\api-key-secret.zip"),
        "inspect",
        operation_id="task-1",
    )
    rendered = json.dumps(invalid, ensure_ascii=False)
    assert invalid["code"] == "data_import_manifest_invalid"
    assert invalid["phase"] == "inspect"
    assert invalid["retryable"] is False
    assert invalid["partial"] is False
    assert invalid["outcome_unknown"] is False
    assert "C:\\private" not in rendered
    assert "api-key-secret" not in rendered

    busy = route_mod._data_transfer_error_diagnostic(error_type("target scope is busy"), "apply")
    assert busy["code"] == "data_transfer_scope_busy"
    assert busy["retryable"] is True
    assert busy["partial"] is False
    assert busy["outcome_unknown"] is False

    unavailable = route_mod._data_transfer_error_diagnostic(error_type("memory store is unavailable"), "apply")
    assert unavailable["code"] == "data_import_memory_unavailable"
    assert unavailable["retryable"] is False
    assert unavailable["partial"] is True
    assert unavailable["outcome_unknown"] is True


def test_unexpected_apply_and_rollback_are_outcome_unknown_without_exception_text() -> None:
    route_mod = load_personification_module("plugin.personification.webui.routes.data_transfer_routes")
    for operation in ("apply", "rollback"):
        report = route_mod._unexpected_diagnostic(RuntimeError(r"token=C:\private\secret"), operation, operation_id="op-1")
        rendered = json.dumps(report, ensure_ascii=False)
        assert report["code"] == f"data_import_{operation}_outcome_unknown"
        assert report["retryable"] is False
        assert report["partial"] is True
        assert report["outcome_unknown"] is True
        assert report["steps"][0]["status"] == "unknown"
        assert "private" not in rendered
        assert "secret" not in rendered


def test_data_transfer_create_route_preserves_result_and_returns_diagnostic(monkeypatch) -> None:
    route_mod = load_personification_module("plugin.personification.webui.routes.data_transfer_routes")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class Bot:
        self_id = "bot1"

        async def get_group_list(self):
            return [{"group_id": "g1"}]

    class Service:
        def create_export(self, **_kwargs):
            return {
                "task_id": "task-1",
                "kind": "export",
                "status": "completed",
                "metadata": {"manifest": {"datasets": ["group_state"]}},
            }

    runtime = type("Runtime", (), {"get_bots": staticmethod(lambda: {"bot1": Bot()})})()
    monkeypatch.setattr(route_mod, "_service", lambda _runtime: Service())
    monkeypatch.setattr(route_mod, "_audit", lambda *_args, **_kwargs: None)
    app = FastAPI()
    app.include_router(route_mod.build_data_transfer_router(runtime=runtime))
    app.dependency_overrides[route_mod.require_admin] = lambda: route_mod.AdminIdentity(qq="1", device_id="device", label="test")
    response = TestClient(app).post("/api/data-transfer/exports/create", json={"bot_id": "bot1", "group_id": "g1"})
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == "task-1"
    assert body["kind"] == "export"
    assert body["status"] == "completed"
    assert body["code"] == "data_export_created"
    assert body["phase"] == "create"
    assert body["steps"][-1]["status"] == "ok"


def test_data_transfer_frontend_renders_operation_diagnostics() -> None:
    source = (Path(__file__).parents[1] / "webui" / "static" / "app-operations.js").read_text(encoding="utf-8")
    assert "renderOperationHistory(items" in source
    assert source.count("operationDiagnosticFromError(") >= 6
    assert '"avatar_relation_evidence","meme_dictionary"' in source
    for legacy in ("打包失败：\"+e.message", "验包失败：\"+e.message", "预演失败：\"+e.message", "导入失败：\"+e.message", "回滚失败：\"+e.message"):
        assert legacy not in source
