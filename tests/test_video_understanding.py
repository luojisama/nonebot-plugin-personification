from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

from ._loader import load_personification_module


video_understanding = load_personification_module("plugin.personification.core.video_understanding")


def _config(preset: str = "balanced", **overrides):  # noqa: ANN001, ANN202
    values = {
        "personification_video_frame_preset": preset,
        "personification_video_visual_soft_limit": 160,
        "personification_video_visual_hard_limit": 192,
        "personification_video_max_scan_samples": 1800,
        "personification_video_contact_sheet_frames": 8,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_three_minute_video_preset_targets_are_model_appropriate() -> None:
    assert video_understanding.resolve_video_frame_budget(180, _config("economy")).target_frames == 72
    assert video_understanding.resolve_video_frame_budget(180, _config("balanced")).target_frames == 120
    assert video_understanding.resolve_video_frame_budget(180, _config("quality")).target_frames == 168


def test_custom_budget_interpolates_and_never_exceeds_hard_limit() -> None:
    config = _config(
        "custom",
        personification_video_custom_frame_budgets={"60": 40, "180": 80, "600": 300},
        personification_video_custom_scan_fps=7,
    )
    middle = video_understanding.resolve_video_frame_budget(120, config)
    long = video_understanding.resolve_video_frame_budget(600, config)
    assert middle.target_frames == 60
    assert middle.scan_fps == 7
    assert long.target_frames == 160  # custom also obeys the configured soft limit


def test_long_video_scan_rate_is_bounded_for_lightweight_server() -> None:
    budget = video_understanding.resolve_video_frame_budget(3600, _config("quality"))
    assert budget.target_frames <= 192
    assert budget.scan_fps == 0.5
    assert budget.scan_fps * 3600 <= budget.max_scan_samples


def test_selection_combines_uniform_scene_and_subtitle_evidence() -> None:
    scores = [(0.01, 0.01) for _ in range(720)]
    scores[111] = (1.0, 0.01)
    scores[333] = (0.01, 1.0)
    selected = video_understanding.select_storyboard_frames(scores, target_frames=120, scan_fps=5.0)
    indices = [item.index for item in selected]
    assert len(indices) == 120
    assert len(set(indices)) == 120
    assert 0 in indices and 719 in indices
    assert 111 in indices and 333 in indices
    assert indices == sorted(indices)


def test_default_contact_sheet_count_for_three_minutes_is_fifteen() -> None:
    budget = video_understanding.resolve_video_frame_budget(180, _config("balanced"))
    assert budget.target_frames == 120
    assert budget.contact_sheet_frames == 8
    assert budget.target_frames // budget.contact_sheet_frames == 15


def test_admin_can_raise_custom_hard_limit_to_256_frames() -> None:
    budget = video_understanding.resolve_video_frame_budget(
        600,
        _config(
            "quality",
            personification_video_visual_hard_limit=256,
            personification_video_visual_soft_limit=224,
        ),
    )
    assert budget.hard_frame_limit == 256
    assert budget.target_frames == 192


def test_social_video_page_routes_are_narrow_and_subtitles_are_deduplicated(tmp_path) -> None:  # noqa: ANN001
    assert video_understanding._social_video_page("https://www.bilibili.com/video/BV1abc123/") == "bilibili"
    assert video_understanding._social_video_page("https://www.douyin.com/video/123456") == "douyin"
    assert video_understanding._social_video_page("https://www.douyin.com/user/123456") == ""
    assert video_understanding._social_video_page("https://evil.example/video/BV1abc123") == ""
    subtitle = tmp_path / "source.zh.vtt"
    subtitle.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n花来\n\n00:00:01.000 --> 00:00:02.000\n花来\n\n00:00:02.000 --> 00:00:03.000\n修脚撤离\n",
        encoding="utf-8",
    )
    assert video_understanding._subtitle_text(subtitle) == "花来 修脚撤离"


def test_ffmpeg_executable_falls_back_to_system_binary(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", None)
    monkeypatch.setattr(video_understanding, "_system_ffmpeg_executable", lambda: "C:/tools/ffmpeg.exe")

    assert video_understanding._ffmpeg_executable() == "C:/tools/ffmpeg.exe"


def test_read_frames_uses_system_backend_when_imageio_binary_is_unavailable(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    class _BrokenImageIoFfmpeg:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            raise RuntimeError("missing bundled ffmpeg")

    sentinel = iter([{"duration": 1.0}])
    captured: dict[str, object] = {}

    def _fallback(path, *, fps, width):  # noqa: ANN001, ANN202
        captured.update(path=path, fps=fps, width=width)
        return sentinel

    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", _BrokenImageIoFfmpeg())
    monkeypatch.setattr(video_understanding, "_system_ffmpeg_read_frames", _fallback)
    path = tmp_path / "video.mp4"

    assert video_understanding._read_frames(path, fps=2.5, width=640) is sentinel
    assert captured == {"path": path, "fps": 2.5, "width": 640}


def test_system_ffprobe_returns_imageio_compatible_metadata(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    payload = {
        "streams": [
            {
                "width": 1920,
                "height": 1080,
                "avg_frame_rate": "24000/1001",
                "duration": "180.250",
            }
        ],
        "format": {"duration": "181.000"},
    }
    captured: list[list[str]] = []

    def _run(command, **kwargs):  # noqa: ANN001, ANN202
        captured.append(list(command))
        assert kwargs == {"capture_output": True, "check": False, "timeout": 30}
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(video_understanding, "_ffprobe_executable", lambda _ffmpeg: "ffprobe-test")
    monkeypatch.setattr(video_understanding.subprocess, "run", _run)

    metadata = video_understanding._system_ffmpeg_probe(tmp_path / "video.mp4", "ffmpeg-test")

    assert metadata["source_size"] == (1920, 1080)
    assert metadata["size"] == (1920, 1080)
    assert metadata["fps"] == 24000 / 1001
    assert metadata["duration"] == 180.25
    assert captured[0][0] == "ffprobe-test"
    assert captured[0][-1] == str(tmp_path / "video.mp4")


def test_system_ffmpeg_reader_streams_complete_rgb_frames_and_closes_pipe(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    frame_size = 4 * 2 * 3
    process_holder: dict[str, object] = {}

    class _Process:
        def __init__(self) -> None:
            self.stdout = io.BytesIO((b"a" * frame_size) + (b"b" * frame_size) + b"partial")

        @staticmethod
        def poll() -> int:
            return 0

    def _popen(command, **kwargs):  # noqa: ANN001, ANN202
        process = _Process()
        process_holder["process"] = process
        process_holder["command"] = list(command)
        assert kwargs["stdin"] is video_understanding.subprocess.DEVNULL
        assert kwargs["stdout"] is video_understanding.subprocess.PIPE
        return process

    monkeypatch.setattr(video_understanding, "_system_ffmpeg_executable", lambda: "ffmpeg-test")
    monkeypatch.setattr(
        video_understanding,
        "_system_ffmpeg_probe",
        lambda _path, _ffmpeg: {"source_size": (4, 2), "size": (4, 2), "fps": 24.0, "duration": 1.0},
    )
    monkeypatch.setattr(video_understanding.subprocess, "Popen", _popen)

    output = list(video_understanding._system_ffmpeg_read_frames(tmp_path / "video.mp4", fps=2.0, width=4))

    assert output[0]["ffmpeg_backend"] == "system"
    assert output[0]["size"] == (4, 2)
    assert output[1:] == [b"a" * frame_size, b"b" * frame_size]
    assert process_holder["process"].stdout.closed is True
    assert process_holder["command"][0] == "ffmpeg-test"
