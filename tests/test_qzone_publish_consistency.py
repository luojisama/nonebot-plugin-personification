from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


data_store = load_personification_module("plugin.personification.core.data_store")
db = load_personification_module("plugin.personification.core.db")
paths = load_personification_module("plugin.personification.core.paths")
qzone_publish = load_personification_module("plugin.personification.core.qzone_publish")
qzone_service = load_personification_module("plugin.personification.core.qzone_service")


class _Logger:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


@pytest.fixture(autouse=True)
def _reset_qzone_auth_state():  # noqa: ANN202
    def _reset() -> None:
        with qzone_service._AUTH_STATE_LOCK:
            qzone_service._AUTH_STATES.clear()

    _reset()
    yield
    _reset()


def _init_store(tmp_path, monkeypatch, state: dict | None = None) -> None:  # noqa: ANN001
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    data_store.init_data_store(SimpleNamespace(personification_data_dir=str(tmp_path)))
    if state is not None:
        data_store.get_data_store().save_sync("qzone_post_state", state)


def test_v1_operation_and_monthly_usage_migration_is_conservative(tmp_path) -> None:
    db_path = tmp_path / "personification.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE kv_store (
                namespace TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                updated_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(namespace, key)
            )
            """
        )
        conn.execute(
            "INSERT INTO kv_store(namespace,key,value) VALUES ('qzone_post_state','__root__',?)",
            (json.dumps({"period": "2026-06", "count": 7, "forward_count": 2}),),
        )
        conn.execute(
            """
            CREATE TABLE qzone_publish_operations (
                operation_id TEXT PRIMARY KEY, period TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, reserved_at REAL NOT NULL, expires_at REAL NOT NULL,
                completed_at REAL NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        for index, status in enumerate(("committed", "unknown", "reserved", "released", "expired")):
            conn.execute(
                "INSERT INTO qzone_publish_operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"legacy-{status}",
                    "2026-06",
                    "post",
                    status,
                    100 + index,
                    200 + index,
                    300 + index,
                    '{"raw_response":"p_skey=must-not-survive"}',
                ),
            )
        conn.commit()

    db.init_db_sync(tmp_path)

    with db.connect_sync(db_path) as conn:
        statuses = {
            row["operation_id"]: row["status"]
            for row in conn.execute("SELECT operation_id,status FROM qzone_publish_operations")
        }
        usage = conn.execute("SELECT * FROM qzone_monthly_usage WHERE period='2026-06'").fetchone()
        serialized = "\n".join(
            str(row["payload_json"]) + str(row["detail"])
            for row in conn.execute("SELECT payload_json,detail FROM qzone_publish_operations")
        )

    assert statuses == {
        "legacy-committed": "succeeded",
        "legacy-unknown": "unknown",
        "legacy-reserved": "unknown",
        "legacy-released": "definite_failure",
        "legacy-expired": "definite_failure",
    }
    assert usage["confirmed_count"] == 7
    assert usage["forward_count"] == 2
    assert "must-not-survive" not in serialized


def test_payload_hash_binds_bot_kind_content_images_and_identity_without_base64(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)
    content = "正文 [IMAGE_B64]QUJD[/IMAGE_B64]"
    reserved = qzone_publish.reserve_qzone_publish(
        operation_id="payload-op",
        bot_id="10001",
        content=content,
        payload_identity={"feed_id": "f1", "raw_response": "secret", "cookie": "p_skey=x"},
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        kind="forward",
    )
    operation = qzone_publish.get_qzone_publish_operation("payload-op")

    assert reserved["ok"] is True
    assert operation is not None
    assert operation["payload"]["image_hashes"]
    assert "QUJD" not in json.dumps(operation["payload"], ensure_ascii=False)
    assert "raw_response" not in operation["payload"]["identity"]
    assert "cookie" not in operation["payload"]["identity"]
    assert qzone_publish.qzone_content_preview(content) == "正文 [配图]"

    mismatch = qzone_publish.reserve_qzone_publish(
        operation_id="payload-op",
        bot_id="10002",
        content=content,
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        kind="forward",
    )
    unresolved = qzone_publish.reserve_qzone_publish(
        operation_id="payload-op-2",
        bot_id="10001",
        content=content,
        payload_identity={"feed_id": "f1"},
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        kind="forward",
    )

    assert mismatch["status"] == "payload_conflict"
    assert unresolved["status"] == "unresolved_payload"


def test_reserved_reclaim_and_fence_cas_rejects_stale_owner(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)
    first = qzone_publish.reserve_qzone_publish(
        operation_id="fenced-op",
        bot_id="10001",
        content="同一正文",
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        lease_seconds=30,
    )
    reclaimed = qzone_publish.reserve_qzone_publish(
        operation_id="fenced-op",
        bot_id="10001",
        content="同一正文",
        now=now + timedelta(seconds=31),
        monthly_limit=5,
        min_interval_hours=0,
        lease_seconds=30,
    )
    assert first["fence_token"] == 1
    assert reclaimed["fence_token"] == 2
    assert qzone_publish._claim_dispatch(
        operation_id="fenced-op",
        fence_token=2,
        now_ts=(now + timedelta(seconds=31)).timestamp(),
        lease_seconds=30,
    )

    stale = qzone_publish._finalize_dispatch(
        operation_id="fenced-op",
        fence_token=1,
        content="同一正文",
        now_ts=(now + timedelta(seconds=32)).timestamp(),
        status="succeeded",
        result_code="code_0",
        resolution_source="test",
        detail={},
    )
    current = qzone_publish.get_qzone_publish_operation("fenced-op")

    assert stale["newly_committed"] is False
    assert current is not None and current["status"] == "dispatching"


def test_expired_reserved_operation_does_not_hold_quota_or_bypass_new_owner(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)
    qzone_publish.reserve_qzone_publish(
        operation_id="expired-reservation",
        bot_id="10001",
        content="尚未开始外发",
        now=now,
        monthly_limit=1,
        min_interval_hours=0,
        lease_seconds=30,
    )

    later = now + timedelta(seconds=31)
    quota = qzone_publish.build_qzone_quota(
        state={"period": "2026-06", "count": 0},
        now=later,
        monthly_limit=1,
        min_interval_hours=0,
    )
    replacement = qzone_publish.reserve_qzone_publish(
        operation_id="replacement-reservation",
        bot_id="10001",
        content="尚未开始外发",
        now=later,
        monthly_limit=1,
        min_interval_hours=0,
    )
    stale_reclaim = qzone_publish.reserve_qzone_publish(
        operation_id="expired-reservation",
        bot_id="10001",
        content="尚未开始外发",
        now=later,
        monthly_limit=1,
        min_interval_hours=0,
    )

    assert quota["held"] == 0 and quota["available"] == 1
    assert replacement["ok"] is True
    assert stale_reclaim["status"] == "unresolved_payload"


def test_unknown_is_held_forever_and_same_payload_cannot_change_id(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)

    async def _unknown():
        return qzone_service.QzoneWriteResult("unknown", "outcome_unknown", "timeout")

    result = asyncio.run(
        qzone_publish.coordinated_qzone_publish(
            operation_id="unknown-op",
            bot_id="10001",
            content="结果未知正文",
            now=now,
            monthly_limit=2,
            min_interval_hours=0,
            kind="post",
            publish=_unknown,
        )
    )
    called = False

    async def _must_not_run():
        nonlocal called
        called = True
        return True, "ok"

    duplicate = asyncio.run(
        qzone_publish.coordinated_qzone_publish(
            operation_id="unknown-op",
            bot_id="10001",
            content="结果未知正文",
            now=now + timedelta(days=60),
            monthly_limit=2,
            min_interval_hours=0,
            kind="post",
            publish=_must_not_run,
        )
    )
    changed_id = qzone_publish.reserve_qzone_publish(
        operation_id="unknown-op-new-id",
        bot_id="10001",
        content="结果未知正文",
        now=now,
        monthly_limit=2,
        min_interval_hours=0,
        kind="post",
    )
    quota = qzone_publish.build_qzone_quota(
        state={"period": "2026-06", "count": 0},
        now=now,
        monthly_limit=2,
        min_interval_hours=0,
    )

    assert result["status"] == "unknown"
    assert duplicate["status"] == "unknown" and duplicate["duplicate"] is True
    assert changed_id["status"] == "unresolved_payload"
    assert called is False
    assert quota["confirmed"] == 0 and quota["held"] == 1 and quota["available"] == 1


def test_coordinator_returns_only_sanitized_publish_detail(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})

    async def _failed():
        return qzone_service.QzoneWriteResult(
            "definite_failure",
            "upload failed",
            "image_upload_missing_pic_bo",
            detail={
                "image_requested": True,
                "image_uploaded": False,
                "cookie": "p_skey=must-not-leak",
                "nested": {"b64_json": "must-not-leak", "status_code": 200},
            },
        )

    result = asyncio.run(qzone_publish.coordinated_qzone_publish(
        operation_id="safe-publish-detail",
        bot_id="10001",
        content="安全诊断正文",
        now=datetime(2026, 6, 10, 12, 0),
        monthly_limit=5,
        min_interval_hours=0,
        kind="post",
        publish=_failed,
    ))

    assert result["status"] == "definite_failure"
    assert result["result_code"] == "image_upload_missing_pic_bo"
    assert result["publish_detail"] == {
        "image_requested": True,
        "image_uploaded": False,
        "nested": {"status_code": 200},
    }
    assert "must-not-leak" not in json.dumps(result, ensure_ascii=False)


def test_success_transition_is_atomic_and_does_not_roll_state_period_back(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    july = datetime(2026, 7, 2, 12, 0)
    june = datetime(2026, 6, 30, 23, 58)
    _init_store(
        tmp_path,
        monkeypatch,
        {
            "period": "2026-07",
            "count": 2,
            "forward_count": 1,
            "last_post_at": july.timestamp(),
            "last_content": "七月原有正文",
            "recent_contents": ["七月原有正文"],
        },
    )

    remote_ids = iter(("remote-june", "remote-july", "remote-july"))

    async def _success():
        return qzone_service.QzoneWriteResult("succeeded", "ok", "code_0", remote_id=next(remote_ids))

    old_month = asyncio.run(
        qzone_publish.coordinated_qzone_publish(
            operation_id="june-late-confirmation",
            bot_id="10001",
            content="六月末的正文",
            now=june,
            monthly_limit=0,
            min_interval_hours=0,
            kind="forward",
            publish=_success,
            force=True,
        )
    )
    state = data_store.get_data_store().load_sync("qzone_post_state")
    with db.connect_sync() as conn:
        june_usage = conn.execute("SELECT * FROM qzone_monthly_usage WHERE period='2026-06'").fetchone()

    assert old_month["newly_committed"] is True
    assert state["period"] == "2026-07"
    assert state["count"] == 2 and state["forward_count"] == 1
    assert state["last_content"] == "七月原有正文"
    assert june_usage["confirmed_count"] == 1 and june_usage["forward_count"] == 1

    july_result = asyncio.run(
        qzone_publish.coordinated_qzone_publish(
            operation_id="july-success",
            bot_id="10001",
            content="七月正文",
            now=july,
            monthly_limit=5,
            min_interval_hours=0,
            kind="post",
            publish=_success,
        )
    )
    duplicate = asyncio.run(
        qzone_publish.coordinated_qzone_publish(
            operation_id="july-success",
            bot_id="10001",
            content="七月正文",
            now=july,
            monthly_limit=5,
            min_interval_hours=0,
            kind="post",
            publish=_success,
        )
    )
    state = data_store.get_data_store().load_sync("qzone_post_state")

    assert july_result["newly_committed"] is True
    assert duplicate["success"] is True and duplicate["newly_committed"] is False
    assert state["period"] == "2026-07" and state["count"] == 3


def test_monthly_limit_zero_remains_zero(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)
    quota = qzone_publish.build_qzone_quota(
        state={"period": "2026-06", "count": 0},
        now=now,
        monthly_limit=0,
        min_interval_hours=0,
    )
    blocked = qzone_publish.reserve_qzone_publish(
        operation_id="zero-limit",
        bot_id="10001",
        content="不应发布",
        now=now,
        monthly_limit=0,
        min_interval_hours=0,
    )

    assert quota["limit"] == 0 and quota["available"] == 0
    assert blocked["status"] == "quota_blocked"


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (SimpleNamespace(status_code=200, text='{"code":0,"tid":"remote-id"}'), "succeeded"),
        (SimpleNamespace(status_code=200, text='{"code":-1,"message":"rejected"}'), "definite_failure"),
        (SimpleNamespace(status_code=200, text=""), "unknown"),
        (SimpleNamespace(status_code=200, text="<html>login.qzone.qq.com</html>"), "unknown"),
        (SimpleNamespace(status_code=408, text="timeout"), "unknown"),
        (SimpleNamespace(status_code=409, text="conflict"), "unknown"),
        (SimpleNamespace(status_code=425, text="early"), "unknown"),
        (SimpleNamespace(status_code=429, text="limited"), "unknown"),
        (SimpleNamespace(status_code=503, text="down"), "unknown"),
    ],
)
def test_publish_service_returns_structured_write_classification(monkeypatch, response, expected) -> None:  # noqa: ANN001
    class _Client:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def post(self, *_args, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            return response

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    _, publish, _ = qzone_service.build_qzone_services(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o10001; skey=sk; p_skey=ps;",
        ),
        _Logger(),
    )
    result = asyncio.run(
        publish("结构化正文", "10001", allow_unknown_write=True)
    )

    assert isinstance(result, qzone_service.QzoneWriteResult)
    assert result.status == expected
    capability = qzone_service.get_qzone_capability_status("10001", enabled=True)
    expected_capability = {
        "succeeded": "available",
        "definite_failure": "unavailable",
        "unknown": "degraded",
    }[expected]
    assert capability["qzone.web_write"]["state"] == expected_capability


def test_unverified_write_is_read_only_until_explicit_manual_attempt() -> None:
    _, publish, _ = qzone_service.build_qzone_services(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o10001; skey=sk; p_skey=ps;",
        ),
        _Logger(),
    )

    result = asyncio.run(publish("周期任务不得探测写能力", "10001"))

    assert result.status == "definite_failure"
    assert result.result_code == "preflight_web_write_unknown"
    capability = qzone_service.get_qzone_capability_status("10001", enabled=True)
    assert capability["qzone.web_write"]["state"] == "unknown"
    assert capability["write_available"] is False


def test_qzone_gif_image_keeps_real_type_and_publishes_complete_media_fields(monkeypatch) -> None:  # noqa: ANN001
    marker = "R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    calls: list[dict] = []

    class _Response:
        status_code = 200

        def __init__(self, text: str) -> None:
            self.text = text

    class _Client:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            calls.append({"client": kwargs})

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def post(self, url, *, params=None, data=None, headers=None):  # noqa: ANN001, ANN201
            calls.append({"url": url, "params": params, "data": data, "headers": headers})
            if "upload" in url:
                return _Response(json.dumps({
                    "ret": 0,
                    "data": {
                        "albumid": "album-1",
                        "lloc": "lloc-1",
                        "sloc": "sloc-1",
                        "type": 1,
                        "height": 32,
                        "width": 32,
                        "pre": "https://example.invalid/photo?bo=pic-bo-1",
                    },
                }))
            return _Response('{"code":0,"tid":"remote-with-image"}')

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    _, publish, _ = qzone_service.build_qzone_services(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o10001; p_uin=o10001; skey=sk; p_skey=ps; pt4_token=pt;",
        ),
        _Logger(),
    )

    result = asyncio.run(
        publish(
            f"结构化配图正文 [IMAGE_B64]{marker}[/IMAGE_B64]",
            "10001",
            allow_unknown_write=True,
        )
    )
    requests = [item for item in calls if "url" in item]

    assert result.status == "succeeded"
    assert result.remote_id == "remote-with-image"
    assert result.detail["image_requested"] is True
    assert result.detail["image_uploaded"] is True
    assert result.detail["image_mime_type"] in {"image/gif", "image/png"}
    assert isinstance(result.detail["image_converted"], bool)
    assert len(requests) == 2
    upload = requests[0]
    assert upload["data"]["base64"] == "1"
    uploaded_bytes = base64.b64decode(upload["data"]["picfile"])
    if result.detail["image_mime_type"] == "image/png":
        assert upload["data"]["filename"] == "qzone.png"
        assert uploaded_bytes.startswith(b"\x89PNG\r\n\x1a\n")
    else:
        assert upload["data"]["filename"] == "qzone.gif"
        assert uploaded_bytes.startswith(b"GIF89a")
    assert upload["headers"]["Cookie"].endswith("pt4_token=pt;")
    posted = requests[1]
    assert posted["params"] == {"g_tk": str(qzone_service._get_g_tk("ps")), "uin": "10001"}
    assert posted["data"]["syn_tweet_verson"] == "1"
    assert posted["data"]["feedversion"] == posted["data"]["ver"] == "1"
    assert posted["data"]["to_sign"] == "0"
    assert posted["data"]["code_version"] == "1"
    assert posted["data"]["pic_bo"] == "pic-bo-1"
    assert posted["data"]["richval"] == ",album-1,lloc-1,sloc-1,1,32,32,,32,32"


def test_qzone_image_upload_failure_stops_before_publish(monkeypatch) -> None:  # noqa: ANN001
    marker = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    request_count = 0

    class _Client:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def post(self, *_args, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            nonlocal request_count
            request_count += 1
            return SimpleNamespace(status_code=200, text='{"ret":-1,"message":"raw secret"}')

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    _, publish, _ = qzone_service.build_qzone_services(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o10001; skey=sk; p_skey=ps;",
        ),
        _Logger(),
    )

    result = asyncio.run(
        publish(
            f"图片失败正文 [IMAGE_B64]{marker}[/IMAGE_B64]",
            "10001",
            allow_unknown_write=True,
        )
    )

    assert result.status == "definite_failure"
    assert result.result_code == "image_upload_rejected"
    assert result.detail == {"image_requested": True, "image_uploaded": False}
    assert "raw secret" not in result.message
    assert request_count == 1


def test_publish_network_interruption_after_post_is_unknown(monkeypatch) -> None:  # noqa: ANN001
    class _Client:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            return None

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args) -> None:  # noqa: ANN003
            return None

        async def post(self, *_args, **_kwargs):  # noqa: ANN001, ANN003, ANN201
            raise qzone_service.httpx.ReadError("response lost")

    monkeypatch.setattr(qzone_service.httpx, "AsyncClient", _Client)
    _, publish, _ = qzone_service.build_qzone_services(
        SimpleNamespace(
            personification_qzone_enabled=True,
            personification_qzone_cookie="uin=o10001; p_skey=ps;",
        ),
        _Logger(),
    )

    result = asyncio.run(
        publish("网络中断正文", "10001", allow_unknown_write=True)
    )
    assert result.status == "unknown"


def test_qzone_write_page_classification_is_precise_and_scoped_per_bot() -> None:
    login = qzone_service._classify_qzone_write_response(
        SimpleNamespace(status_code=200, text="<html>login.qzone.qq.com</html>"),
        action="发布",
        bot_id="10001",
    )
    risk = qzone_service._classify_qzone_write_response(
        SimpleNamespace(status_code=200, text="<html>安全验证 验证码</html>"),
        action="发布",
        bot_id="10002",
    )
    generic = qzone_service._classify_qzone_write_response(
        SimpleNamespace(status_code=200, text="<html>temporary gateway page</html>"),
        action="发布",
        bot_id="10003",
    )

    assert login.result_code == "auth_page_2xx"
    assert risk.result_code == "risk_page_2xx"
    assert generic.result_code == "html_response_2xx"
    assert qzone_service.get_qzone_auth_status("10001")["status"] == "login_required"
    assert qzone_service.get_qzone_auth_status("10002")["status"] == "risk_blocked"
    assert qzone_service.get_qzone_auth_status("10003")["status"] == "unknown"


def test_reconcile_requires_unique_exact_content_hash_bot_and_time(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)

    async def _unknown():
        return qzone_service.QzoneWriteResult("unknown", "lost", "timeout")

    for operation_id, content in (
        ("reconcile-one", "精确正文"),
        ("reconcile-many", "重复正文"),
        ("reconcile-helper", "远端 helper 正文"),
    ):
        asyncio.run(
            qzone_publish.coordinated_qzone_publish(
                operation_id=operation_id,
                bot_id="10001",
                content=content,
                now=now,
                monthly_limit=5,
                min_interval_hours=0,
                kind="post",
                publish=_unknown,
            )
        )

    exact = qzone_publish.reconcile_qzone_publish_operation(
        operation_id="reconcile-one",
        bot_id="10001",
        feeds=[
            {
                "feed_id": "remote-exact",
                "owner_uin": "10001",
                "content": "精确正文",
                "created_at": now.timestamp() + 5,
            },
            {
                "feed_id": "wrong-bot",
                "owner_uin": "10002",
                "content": "精确正文",
                "created_at": now.timestamp() + 5,
            },
        ],
    )
    ambiguous = qzone_publish.reconcile_qzone_publish_operation(
        operation_id="reconcile-many",
        bot_id="10001",
        feeds=[
            {"feed_id": "a", "owner_uin": "10001", "content": "重复正文", "created_at": now.timestamp()},
            {"feed_id": "b", "owner_uin": "10001", "content": "重复正文", "created_at": now.timestamp() + 1},
        ],
    )

    class _SelfFeedService:
        async def fetch_user_feeds(self, **_kwargs):  # noqa: ANN003
            return True, "ok", [
                {
                    "feed_id": "remote-helper",
                    "owner_uin": "10001",
                    "content": "远端 helper 正文",
                    "created_at": now.timestamp() + 2,
                }
            ]

    helper = asyncio.run(
        qzone_publish.reconcile_qzone_publish_from_self_feed(
            operation_id="reconcile-helper",
            bot_id="10001",
            qzone_social_service=_SelfFeedService(),
        )
    )

    assert exact["status"] == "succeeded" and exact["remote_id"] == "remote-exact"
    assert ambiguous["status"] == "unknown" and ambiguous["match_count"] == 2
    assert helper["status"] == "succeeded" and helper["remote_id"] == "remote-helper"
    assert qzone_publish.get_qzone_publish_operation("reconcile-many")["status"] == "unknown"


def test_forward_reconcile_requires_original_feed_identity(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-06", "count": 0})
    now = datetime(2026, 6, 10, 12, 0)

    async def _unknown():
        return qzone_service.QzoneWriteResult("unknown", "lost", "timeout")

    asyncio.run(qzone_publish.coordinated_qzone_publish(
        operation_id="forward-reconcile",
        bot_id="10001",
        content="转发附言",
        payload_identity={
            "owner_uin": "20001",
            "feed_id": "source-feed",
            "topic_id": "source-topic",
            "appid": "311",
        },
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        kind="forward",
        publish=_unknown,
    ))

    wrong = qzone_publish.reconcile_qzone_publish_operation(
        operation_id="forward-reconcile",
        bot_id="10001",
        feeds=[{
            "feed_id": "remote-wrong",
            "owner_uin": "10001",
            "content": "转发附言",
            "created_at": now.timestamp(),
            "raw": {"rt_uin": "20001", "rt_tid": "other-feed", "rt_topicid": "source-topic", "rt_appid": "311"},
        }],
    )
    exact = qzone_publish.reconcile_qzone_publish_operation(
        operation_id="forward-reconcile",
        bot_id="10001",
        feeds=[{
            "feed_id": "remote-forward",
            "owner_uin": "10001",
            "content": "转发附言",
            "created_at": now.timestamp(),
            "raw": {"rt_uin": "20001", "rt_tid": "source-feed", "rt_topicid": "source-topic", "rt_appid": "311"},
        }],
    )

    assert wrong["status"] == "unknown" and wrong["match_count"] == 0
    assert exact["status"] == "succeeded" and exact["remote_id"] == "remote-forward"


def test_admin_resolution_and_historical_feed_reconciliation_are_idempotent(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    _init_store(tmp_path, monkeypatch, {"period": "2026-07", "count": 2, "recent_contents": []})
    now = datetime(2026, 7, 13, 5, 0)

    async def _unknown():
        return qzone_service.QzoneWriteResult("unknown", "lost", "timeout")

    asyncio.run(qzone_publish.coordinated_qzone_publish(
        operation_id="admin-resolve",
        bot_id="10001",
        content="远端实际不存在",
        now=now,
        monthly_limit=5,
        min_interval_hours=0,
        kind="post",
        publish=_unknown,
    ))
    resolved = qzone_publish.resolve_qzone_publish_absent(
        operation_id="admin-resolve",
        bot_id="10001",
        now=now + timedelta(minutes=1),
    )
    assert resolved["status"] == "definite_failure" and resolved["changed"] is True

    feed = {
        "feed_id": "remote-0448",
        "owner_uin": "10001",
        "content": "突然想把游戏里的默认头像换掉，可原来的看久了也没那么难看",
        "created_at": datetime(2026, 7, 13, 4, 48).timestamp(),
    }
    first = qzone_publish.record_historical_qzone_feed(
        bot_id="10001",
        feed=feed,
        occurred_at=datetime(2026, 7, 13, 4, 48),
        resolved_at=now,
    )
    duplicate = qzone_publish.record_historical_qzone_feed(
        bot_id="10001",
        feed=feed,
        occurred_at=datetime(2026, 7, 13, 4, 48),
        resolved_at=now,
    )
    state = data_store.get_data_store().load_sync("qzone_post_state")

    assert first["newly_committed"] is True
    assert duplicate["duplicate"] is True and duplicate["newly_committed"] is False
    assert state["count"] == 3
    assert state["last_content"] == feed["content"]
