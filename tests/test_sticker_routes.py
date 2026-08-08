from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


@pytest.fixture
def _runtime_with_stickers(tmp_path: Path, monkeypatch):
    data_store = load_personification_module("plugin.personification.core.data_store")
    paths = load_personification_module("plugin.personification.core.paths")
    monkeypatch.setattr(paths, "get_data_dir", lambda _cfg=None: tmp_path)
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    # 创建 3 个假表情包 + 一个 stickers.json
    (sticker_dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    (sticker_dir / "b.jpg").write_bytes(b"\xff\xd8\xff" + b"0" * 100)
    (sticker_dir / "c.gif").write_bytes(b"GIF89a" + b"0" * 100)
    manifest = {
        "a.png": {
            "description": "笑脸",
            "mood_tags": ["开心"],
            "scene_tags": ["表达情绪"],
            "weight": 1.0,
        },
        "b.jpg": {},  # 未打标
    }
    (sticker_dir / "stickers.json").write_text(json.dumps(manifest), encoding="utf-8")

    cfg = SimpleNamespace(
        personification_data_dir=str(tmp_path),
        personification_sticker_path=str(sticker_dir),
    )
    data_store.init_data_store(cfg)

    app_module = load_personification_module("plugin.personification.webui.app")
    app_module.set_runtime_context(
        plugin_config=cfg,
        superusers={"10001"},
        get_bots=lambda: {"1": SimpleNamespace()},
        logger=SimpleNamespace(info=lambda *_a, **_k: None, warning=lambda *_a, **_k: None),
        runtime_bundle=SimpleNamespace(),
    )
    return SimpleNamespace(plugin_config=cfg, app_module=app_module, sticker_dir=sticker_dir)


def _build_client(rt):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(rt.app_module.build_router())
    return TestClient(app)


def _login(client, rt) -> None:
    sent: list = []

    class _Bot:
        async def call_api(self, _n: str, **kwargs):
            sent.append(kwargs)
            return {"message_id": 1}

    rt.app_module.get_runtime_context().get_bots = lambda: {"1": _Bot()}
    res = client.post("/personification/api/auth/login", json={"qq": "10001"})
    assert res.status_code == 200, res.text
    code = re.search(r"\b(\d{6})\b", str(sent[-1].get("message", ""))).group(1)
    res2 = client.post("/personification/api/auth/verify", json={"qq": "10001", "code": code, "device_label": "t"})
    assert res2.status_code == 200, res2.text
    csrf = client.cookies.get("personification_webui_csrf", "")
    if csrf:
        client.headers["X-Personification-CSRF"] = csrf


def test_list_stickers_returns_files_and_labels(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.get("/personification/api/stickers")
    assert res.status_code == 200
    body = res.json()
    names = {s["filename"]: s for s in body["stickers"]}
    assert {"a.png", "b.jpg", "c.gif"} <= set(names)
    assert names["a.png"]["labeled"] is True
    assert names["a.png"]["description"] == "笑脸"
    assert names["b.jpg"]["labeled"] is False
    assert body["total"] == 3
    assert body["labeled_count"] == 1


def test_get_sticker_file_streams_image(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.get("/personification/api/stickers/file/a.png")
    assert res.status_code == 200
    assert b"\x89PNG" in res.content[:8]


def test_path_traversal_blocked(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    # 经过 URL 解码后 .. 直接被路由拦截
    res = client.get("/personification/api/stickers/file/..%2Fsensitive.txt")
    assert res.status_code in (400, 404)
    res2 = client.get("/personification/api/stickers/file/a%2Fb")
    assert res2.status_code in (400, 404)


def test_patch_sticker_updates_metadata(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.patch(
        "/personification/api/stickers/a.png",
        json={"description": "新的描述", "weight": 2.0, "proactive_send": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["code"] == "sticker_metadata_saved"
    assert body["diagnostic"]["steps"][-1]["status"] == "ok"
    assert body["entry"]["description"] == "新的描述"
    assert body["entry"]["weight"] == 2.0
    assert body["entry"]["proactive_send"] is True


def test_delete_moves_to_trash(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.delete("/personification/api/stickers/c.gif")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["code"] == "sticker_deleted"
    assert body["trash_path"].startswith("trash/")
    assert str(_runtime_with_stickers.sticker_dir) not in body["trash_path"]
    assert not (_runtime_with_stickers.sticker_dir / "c.gif").exists()
    # 从 manifest 移除
    manifest = json.loads((_runtime_with_stickers.sticker_dir / "stickers.json").read_text(encoding="utf-8"))
    assert "c.gif" not in manifest


def test_upload_creates_file_and_manifest_entry(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    payload = b"\x89PNG\r\n\x1a\n" + b"abc" * 50
    res = client.post(
        "/personification/api/stickers/upload",
        files={"file": ("new_sticker.png", payload, "image/png")},
        data={"description": "用户描述"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    assert body["code"] == "sticker_uploaded"
    assert [step["status"] for step in body["steps"]] == ["ok", "ok", "ok"]
    assert (_runtime_with_stickers.sticker_dir / body["filename"]).exists()
    manifest = json.loads((_runtime_with_stickers.sticker_dir / "stickers.json").read_text(encoding="utf-8"))
    assert manifest[body["filename"]]["description"] == "用户描述"


def test_upload_rejects_oversized_file(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    payload = b"x" * (5 * 1024 * 1024)  # 5MB > 4MB 上限
    res = client.post(
        "/personification/api/stickers/upload",
        files={"file": ("big.png", payload, "image/png")},
    )
    assert res.status_code == 413
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_upload_too_large"
    assert detail["phase"] == "file_validation"
    assert detail["steps"][1]["status"] == "skipped"


def test_rescan_force_all_clears_all_entries(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.post("/personification/api/stickers/rescan", json={"mode": "force_all"})
    assert res.status_code == 200, res.text
    assert res.json()["code"] == "sticker_rescan_scheduled"
    assert res.json()["diagnostic"]["steps"][-1]["status"] == "ok"
    assert res.json()["scheduled"] >= 1
    manifest = json.loads((_runtime_with_stickers.sticker_dir / "stickers.json").read_text(encoding="utf-8"))
    # 所有有 manifest 的条目都被清空
    assert all(v == {} for v in manifest.values())


def test_rescan_missing_only_keeps_labeled(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.post("/personification/api/stickers/rescan", json={"mode": "missing_only"})
    assert res.status_code == 200, res.text
    manifest = json.loads((_runtime_with_stickers.sticker_dir / "stickers.json").read_text(encoding="utf-8"))
    # a.png 有 description 应当被保留
    assert manifest["a.png"].get("description") == "笑脸"
    # b.jpg 没 description 应当被清空（为重新打标准备）
    assert manifest["b.jpg"] == {}


def test_upload_rejects_invalid_filename_with_structured_diagnostic(_runtime_with_stickers) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    res = client.post(
        "/personification/api/stickers/upload",
        files={"file": ("../secret.png", b"valid-bytes", "image/png")},
    )
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_invalid_filename"
    assert detail["phase"] == "file_validation"
    assert detail["retryable"] is True
    assert "secret.png" not in str(detail)


def test_upload_write_error_is_structured_and_redacted(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    secret = "D:/private/api_key=super-secret"

    def _fail_write(_path, _payload):
        raise PermissionError(secret)

    monkeypatch.setattr(route_mod.Path, "write_bytes", _fail_write)
    res = client.post(
        "/personification/api/stickers/upload",
        files={"file": ("write-fail.png", b"valid-bytes", "image/png")},
    )
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_upload_write_failed"
    assert detail["phase"] == "file_write"
    assert detail["partial"] is False
    assert detail["steps"][1]["status"] == "error"
    assert secret not in str(detail)


def test_upload_metadata_error_reports_partial_outcome(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    secret = "D:/private/stickers.json token=super-secret"
    monkeypatch.setattr(
        route_mod,
        "save_sticker_metadata_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
    )
    res = client.post(
        "/personification/api/stickers/upload",
        files={"file": ("metadata-fail.png", b"valid-bytes", "image/png")},
    )
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_upload_metadata_partial"
    assert detail["phase"] == "metadata_save"
    assert detail["partial"] is True
    assert detail["retryable"] is False
    assert [step["status"] for step in detail["steps"]] == ["ok", "ok", "error"]
    assert (_runtime_with_stickers.sticker_dir / "metadata-fail.png").exists()
    assert secret not in str(detail)
    assert str(_runtime_with_stickers.sticker_dir) not in str(detail)


def test_patch_manifest_error_is_structured_and_outcome_unknown(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    secret = "D:/private/stickers.json password=super-secret"
    monkeypatch.setattr(
        route_mod,
        "save_sticker_metadata_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
    )
    res = client.patch(
        "/personification/api/stickers/a.png",
        json={"description": "不会确认保存"},
    )
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_metadata_save_failed"
    assert detail["phase"] == "metadata_save"
    assert detail["outcome_unknown"] is True
    assert detail["steps"][-1]["status"] == "error"
    assert secret not in str(detail)


def test_delete_move_error_is_structured_and_keeps_manifest(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    secret = "D:/private/trash password=super-secret"
    monkeypatch.setattr(
        route_mod.shutil,
        "move",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(secret)),
    )
    res = client.delete("/personification/api/stickers/a.png")
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_trash_move_failed"
    assert detail["phase"] == "trash_move"
    assert detail["partial"] is False
    assert detail["outcome_unknown"] is False
    assert (_runtime_with_stickers.sticker_dir / "a.png").exists()
    assert secret not in str(detail)


def test_delete_manifest_error_reports_file_move_as_partial(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    secret = "D:/private/stickers.json p_skey=super-secret"
    monkeypatch.setattr(
        route_mod,
        "save_sticker_metadata_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError(secret)),
    )
    res = client.delete("/personification/api/stickers/c.gif")
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_delete_manifest_partial"
    assert detail["phase"] == "manifest_save"
    assert detail["partial"] is True
    assert detail["retryable"] is False
    assert [step["status"] for step in detail["steps"]] == ["ok", "ok", "error"]
    assert not (_runtime_with_stickers.sticker_dir / "c.gif").exists()
    assert "trash/" in str(detail)
    assert str(_runtime_with_stickers.sticker_dir) not in str(detail)
    assert secret not in str(detail)


def test_rescan_filesystem_error_is_structured_and_redacted(_runtime_with_stickers, monkeypatch) -> None:
    client = _build_client(_runtime_with_stickers)
    _login(client, _runtime_with_stickers)
    route_mod = load_personification_module("plugin.personification.webui.routes.sticker_routes")
    original_write_text = route_mod.Path.write_text
    secret = "D:/private/stickers.json api_key=super-secret"

    def _fail_manifest_write(path, *args, **kwargs):
        if path.name == "stickers.json":
            raise PermissionError(secret)
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(route_mod.Path, "write_text", _fail_manifest_write)
    res = client.post("/personification/api/stickers/rescan", json={"mode": "force_all"})
    assert res.status_code == 500
    detail = res.json()["detail"]
    assert detail["code"] == "sticker_rescan_filesystem_failed"
    assert detail["phase"] == "manifest_save"
    assert detail["outcome_unknown"] is True
    assert detail["steps"][1]["status"] == "ok"
    assert detail["steps"][2]["status"] == "unknown"
    assert secret not in str(detail)
    assert str(_runtime_with_stickers.sticker_dir) not in str(detail)


def test_sticker_frontend_persists_and_renders_operation_diagnostics() -> None:
    source = (Path(__file__).parents[1] / "webui" / "static" / "app-content.js").read_text(encoding="utf-8")
    assert "STICKER_DIAGNOSTICS_STORAGE_KEY" in source
    assert "sessionStorage.setItem(STICKER_DIAGNOSTICS_STORAGE_KEY" in source
    assert "renderOperationHistory(stickerDiagnostics()" in source
    assert source.count("rememberStickerDiagnostic(") >= 8
    assert source.count("operationDiagnosticFromError(") >= 4
    assert 'await api("/stickers/upload"' in source
    for legacy in ("上传失败：\" + e.message", "保存失败：\" + e.message", "删除失败：\" + e.message"):
        assert legacy not in source
