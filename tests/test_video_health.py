from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


diagnostics = load_personification_module("plugin.personification.core.diagnostics")
media_understanding = load_personification_module("plugin.personification.core.media_understanding")


def _cfg(**values):
    defaults = {
        "personification_video_understanding_enabled": True,
        "personification_video_route_mode": "auto",
        "personification_video_storyboard_fallback_enabled": True,
        "personification_data_dir": "",
        "personification_health_probe_dir": "",
        "personification_video_analysis_timeout": 30,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_video_understanding_is_a_dedicated_category() -> None:
    assert "视频理解" in diagnostics.CATEGORY_NAMES


def test_disabled_video_probe_never_calls_model(monkeypatch) -> None:
    async def _fail(**_kwargs):
        raise AssertionError("disabled probe must not call the model")

    monkeypatch.setattr(diagnostics, "_probe_video_understanding", _fail)
    cfg = _cfg(personification_video_understanding_enabled=False)
    checks = asyncio.run(
        diagnostics._video_checks(
            cfg,
            SimpleNamespace(reply_processor_deps=SimpleNamespace(runtime=None)),
            None,
            probe_video=True,
        )
    )

    assert next(item for item in checks if item["key"] == "video_config")["status"] == "disabled"
    assert next(item for item in checks if item["key"] == "video_probe")["status"] == "disabled"


def test_video_readiness_reports_invalid_route_and_incomplete_external_fallback(monkeypatch) -> None:
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda cfg, logger: [])
    cfg = _cfg(
        personification_video_route_mode="not-a-route",
        personification_fullmodal_provider_enabled=True,
        personification_fullmodal_provider_protocol="gemini_native",
        personification_fullmodal_provider_model="",
        personification_fullmodal_provider_api_key="",
    )

    checks = asyncio.run(
        diagnostics._video_checks(
            cfg,
            SimpleNamespace(reply_processor_deps=SimpleNamespace(runtime=None)),
            None,
        )
    )
    by_key = {item["key"]: item for item in checks}

    assert by_key["video_route_mode"]["diagnostic_code"] == "video_route_mode_invalid"
    assert by_key["video_external_fallback"]["diagnostic_code"] == "video_external_fallback_incomplete"
    assert by_key["video_route"]["diagnostic_code"] == "video_provider_unconfigured"


def test_full_diagnostics_does_not_probe_video(monkeypatch) -> None:
    seen: list[bool] = []

    async def _fake_category(name, **kwargs):
        if name == "视频理解":
            seen.append(bool(kwargs.get("probe_video")))
        return []

    monkeypatch.setattr(diagnostics, "_run_category", _fake_category)
    cfg = _cfg()
    full = asyncio.run(diagnostics.run_diagnostics(plugin_config=cfg))
    single = asyncio.run(diagnostics.run_diagnostics(plugin_config=cfg, only="视频理解", probe_video=True))

    assert "视频理解" in [item["name"] for item in full["categories"]]
    assert [item["name"] for item in single["categories"]] == ["视频理解"]
    assert seen == [False, True]


def test_video_probe_uses_uploaded_video_in_configured_workspace(monkeypatch, tmp_path: Path) -> None:
    async def _fake_analyze(**kwargs):
        kwargs["route_attempts"].append(
            {"route": "video_primary_gemini", "status": "ok", "elapsed_ms": 3}
        )
        return '{"scene_summary":"红色方块","visual_evidence":["red"]}', "video_primary_gemini"

    monkeypatch.setattr(media_understanding, "analyze_videos_with_route_or_fallback", _fake_analyze)
    monkeypatch.setattr(
        diagnostics,
        "_video_probe_runtime",
        lambda runtime, cfg, providers, logger: SimpleNamespace(
            plugin_config=cfg,
            logger=logger,
            get_configured_api_providers=lambda: providers,
        ),
    )
    provider = {
        "name": "video-main",
        "api_type": "gemini_official",
        "api_key": "configured-but-never-displayed",
        "model": "gemini-2.0-flash",
        "media_protocol": "gemini_native",
        "priority": 1,
    }
    monkeypatch.setattr(diagnostics, "_get", lambda cfg, name, default=None: getattr(cfg, name, default))
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(ai_routes, "list_primary_providers", lambda cfg, logger: [provider])

    configured_probe_root = tmp_path / "configured-health-probes"
    cfg = _cfg(personification_health_probe_dir=str(configured_probe_root))
    uploaded_video = configured_probe_root / "upload-test" / "video.mp4"
    uploaded_video.parent.mkdir(parents=True)
    uploaded_video.write_bytes(b"uploaded-test-video")
    checks = asyncio.run(
        diagnostics._video_checks(
            cfg,
            SimpleNamespace(reply_processor_deps=SimpleNamespace(runtime=SimpleNamespace())),
            None,
            probe_video=True,
            video_path=uploaded_video,
        )
    )

    probe = next(item for item in checks if item["key"] == "video_probe")
    assert probe["status"] == "ok"
    assert probe["diagnostic_code"] == "video_probe_ok"
    assert "selected_route=video_primary_gemini" in probe["detail"]
    assert "configured-but-never-displayed" not in str(checks)
    assert uploaded_video.exists()


def test_video_probe_without_upload_does_not_call_model(monkeypatch) -> None:
    async def _fail(**_kwargs):
        raise AssertionError("missing upload must not call the model")

    monkeypatch.setattr(diagnostics, "_probe_video_understanding", _fail)
    ai_routes = load_personification_module("plugin.personification.core.ai_routes")
    monkeypatch.setattr(
        ai_routes,
        "list_primary_providers",
        lambda cfg, logger: [{
            "name": "video-main",
            "api_type": "gemini_official",
            "api_key": "configured",
            "model": "gemini-2.0-flash",
            "media_protocol": "gemini_native",
        }],
    )
    cfg = _cfg()
    checks = asyncio.run(
        diagnostics._video_checks(
            cfg,
            SimpleNamespace(reply_processor_deps=SimpleNamespace(runtime=None)),
            None,
            probe_video=True,
            video_path=None,
        )
    )
    probe = next(item for item in checks if item["key"] == "video_probe")
    assert probe["diagnostic_code"] == "video_probe_upload_required"
