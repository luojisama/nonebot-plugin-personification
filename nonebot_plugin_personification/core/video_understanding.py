from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlsplit

from PIL import Image, ImageChops, ImageDraw, ImageFont

from .media_refs import normalize_video_ref
from .safe_media_download import SafeMediaDownloadError, download_public_media_to_path


_VIDEO_MIMES = {
    "application/octet-stream",
    "video/avi",
    "video/mp4",
    "video/mpeg",
    "video/quicktime",
    "video/webm",
    "video/x-m4v",
    "video/x-matroska",
    "video/x-msvideo",
}
_PRESET_ANCHORS: dict[str, tuple[tuple[float, int], ...]] = {
    "economy": ((15.0, 16), (60.0, 36), (180.0, 72), (600.0, 96)),
    "balanced": ((15.0, 24), (60.0, 60), (180.0, 120), (600.0, 160)),
    "quality": ((15.0, 32), (60.0, 84), (180.0, 168), (600.0, 192)),
}
_PRESET_SCAN_FPS = {"economy": 2.5, "balanced": 5.0, "quality": 7.5}
_DEFAULT_HARD_FRAME_LIMIT = 192
_MAX_HARD_FRAME_LIMIT = 256
_DEFAULT_MAX_SCAN_SAMPLES = 1800
_DEFAULT_CONTACT_SHEET_FRAMES = 8
_DEFAULT_MAX_VIDEO_BYTES = 256 * 1024 * 1024
_DEFAULT_PAYLOAD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class VideoFrameBudget:
    preset: str
    duration_seconds: float
    target_frames: int
    scan_fps: float
    max_scan_samples: int
    contact_sheet_frames: int
    hard_frame_limit: int


@dataclass(frozen=True)
class SelectedFrame:
    index: int
    timestamp_seconds: float
    scene_score: float = 0.0
    subtitle_score: float = 0.0


@dataclass
class VideoStoryboard:
    source_ref: str
    source_url: str
    video_path: Path
    temp_dir: Path
    duration_seconds: float
    source_fps: float
    scan_fps: float
    source_size: tuple[int, int]
    target_frame_count: int
    selected_frames: list[SelectedFrame] = field(default_factory=list)
    frame_paths: list[Path] = field(default_factory=list)
    contact_sheet_paths: list[Path] = field(default_factory=list)
    contact_sheet_refs: list[str] = field(default_factory=list)
    audio_path: Path | None = None
    subtitle_text: str = ""
    warnings: list[str] = field(default_factory=list)

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def summary(self) -> dict[str, Any]:
        return {
            "duration_seconds": round(self.duration_seconds, 3),
            "source_fps": round(self.source_fps, 3),
            "scan_fps": round(self.scan_fps, 3),
            "source_size": list(self.source_size),
            "target_frame_count": self.target_frame_count,
            "selected_frame_count": len(self.selected_frames),
            "contact_sheet_count": len(self.contact_sheet_refs),
            "timestamps_seconds": [round(item.timestamp_seconds, 3) for item in self.selected_frames],
            "warnings": list(self.warnings),
            "subtitle_available": bool(self.subtitle_text),
        }


def normalize_video_frame_preset(value: Any) -> str:
    normalized = str(value or "balanced").strip().lower().replace("-", "_")
    aliases = {"default": "balanced", "normal": "balanced", "low": "economy", "high": "quality"}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"economy", "balanced", "quality", "custom"} else "balanced"


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(maximum, result))


def _interpolate_anchors(duration_seconds: float, anchors: Sequence[tuple[float, int]]) -> int:
    duration = max(0.0, float(duration_seconds or 0.0))
    ordered = sorted((max(0.1, float(sec)), max(1, int(count))) for sec, count in anchors)
    if duration <= ordered[0][0]:
        ratio = max(0.25, duration / ordered[0][0])
        return max(4, int(round(ordered[0][1] * ratio)))
    for (left_sec, left_count), (right_sec, right_count) in zip(ordered, ordered[1:]):
        if duration <= right_sec:
            ratio = (duration - left_sec) / max(0.001, right_sec - left_sec)
            return int(round(left_count + (right_count - left_count) * ratio))
    return ordered[-1][1]


def _custom_anchors(config: Any) -> tuple[tuple[float, int], ...]:
    raw = getattr(config, "personification_video_custom_frame_budgets", {}) or {}
    if not isinstance(raw, dict):
        return _PRESET_ANCHORS["balanced"]
    parsed: list[tuple[float, int]] = []
    for duration, count in raw.items():
        try:
            parsed.append((float(duration), int(count)))
        except (TypeError, ValueError):
            continue
    return tuple(parsed) if parsed else _PRESET_ANCHORS["balanced"]


def resolve_video_frame_budget(duration_seconds: float, config: Any) -> VideoFrameBudget:
    preset = normalize_video_frame_preset(getattr(config, "personification_video_frame_preset", "balanced"))
    hard_limit = _bounded_int(
        getattr(config, "personification_video_visual_hard_limit", _DEFAULT_HARD_FRAME_LIMIT),
        _DEFAULT_HARD_FRAME_LIMIT,
        12,
        _MAX_HARD_FRAME_LIMIT,
    )
    soft_limit = _bounded_int(
        getattr(config, "personification_video_visual_soft_limit", 160),
        160,
        8,
        hard_limit,
    )
    anchors = _custom_anchors(config) if preset == "custom" else _PRESET_ANCHORS[preset]
    target = _interpolate_anchors(duration_seconds, anchors)
    if preset != "quality":
        target = min(target, soft_limit)
    target = max(4, min(hard_limit, target))
    scan_default = 5.0 if preset == "custom" else _PRESET_SCAN_FPS[preset]
    scan_fps = _bounded_float(
        getattr(config, "personification_video_custom_scan_fps", scan_default) if preset == "custom" else scan_default,
        scan_default,
        0.5,
        8.0,
    )
    max_samples = _bounded_int(
        getattr(config, "personification_video_max_scan_samples", _DEFAULT_MAX_SCAN_SAMPLES),
        _DEFAULT_MAX_SCAN_SAMPLES,
        240,
        5000,
    )
    if duration_seconds > 0:
        scan_fps = min(scan_fps, max(0.5, max_samples / float(duration_seconds)))
    sheet_frames = _bounded_int(
        getattr(config, "personification_video_contact_sheet_frames", _DEFAULT_CONTACT_SHEET_FRAMES),
        _DEFAULT_CONTACT_SHEET_FRAMES,
        4,
        9,
    )
    return VideoFrameBudget(
        preset=preset,
        duration_seconds=max(0.0, float(duration_seconds or 0.0)),
        target_frames=target,
        scan_fps=scan_fps,
        max_scan_samples=max_samples,
        contact_sheet_frames=sheet_frames,
        hard_frame_limit=hard_limit,
    )


def _system_ffmpeg_executable() -> str:
    configured = str(os.environ.get("IMAGEIO_FFMPEG_EXE") or "").strip()
    candidates = [configured, "ffmpeg"] if configured else ["ffmpeg"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve())
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.is_file():
            return str(path.resolve())
    raise RuntimeError("video_ffmpeg_unavailable")


def _ffmpeg_executable() -> str:
    try:
        import imageio_ffmpeg

        return str(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return _system_ffmpeg_executable()


def _ffprobe_executable(ffmpeg_executable: str) -> str:
    configured = str(os.environ.get("PERSONIFICATION_FFPROBE_EXE") or "").strip()
    sibling_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    sibling = Path(ffmpeg_executable).resolve().with_name(sibling_name)
    candidates = [configured, str(sibling), "ffprobe"] if configured else [str(sibling), "ffprobe"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return str(Path(resolved).resolve())
        path = Path(candidate).expanduser()
        if path.is_absolute() and path.is_file():
            return str(path.resolve())
    raise RuntimeError("video_ffprobe_unavailable")


def _fraction(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            return float(numerator) / max(0.000001, float(denominator))
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def _system_ffmpeg_probe(path: Path, ffmpeg_executable: str) -> dict[str, Any]:
    command = [
        _ffprobe_executable(ffmpeg_executable),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,avg_frame_rate,r_frame_rate,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("video_storyboard_probe_failed") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise RuntimeError("video_storyboard_probe_failed")
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise RuntimeError("video_storyboard_probe_failed") from exc
    streams = list(payload.get("streams") or []) if isinstance(payload, dict) else []
    stream = streams[0] if streams and isinstance(streams[0], dict) else {}
    format_data = payload.get("format") if isinstance(payload, dict) else {}
    if not isinstance(format_data, dict):
        format_data = {}
    try:
        width = max(0, int(stream.get("width") or 0))
        height = max(0, int(stream.get("height") or 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("video_storyboard_probe_failed") from exc
    if width <= 0 or height <= 0:
        raise RuntimeError("video_storyboard_probe_failed")
    fps = _fraction(stream.get("avg_frame_rate")) or _fraction(stream.get("r_frame_rate"))
    duration = _fraction(stream.get("duration")) or _fraction(format_data.get("duration"))
    return {
        "source_size": (width, height),
        "size": (width, height),
        "fps": fps,
        "duration": duration,
    }


def _read_exact(stream: io.BufferedReader, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max(0, int(size))
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _system_ffmpeg_read_frames(path: Path, *, fps: float, width: int):
    ffmpeg = _system_ffmpeg_executable()
    metadata = _system_ffmpeg_probe(path, ffmpeg)
    source_width, source_height = tuple(metadata["source_size"])
    output_width = max(2, min(max(2, int(width)), int(source_width)))
    output_height = max(2, int(round(source_height * output_width / max(1, source_width))))
    if output_height % 2:
        output_height += 1
    frame_size = output_width * output_height * 3
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"fps={float(fps):.6f},scale={output_width}:{output_height}:flags=fast_bilinear",
        "-pix_fmt",
        "rgb24",
        "-f",
        "rawvideo",
        "pipe:1",
    ]

    def _frames():
        try:
            process = subprocess.Popen(  # noqa: S603 - argv is passed without a command shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError("video_storyboard_scan_failed") from exc
        try:
            if process.stdout is None:
                raise RuntimeError("video_storyboard_scan_failed")
            yield {
                **metadata,
                "size": (output_width, output_height),
                "ffmpeg_backend": "system",
            }
            while True:
                frame = _read_exact(process.stdout, frame_size)
                if len(frame) != frame_size:
                    break
                yield frame
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    return _frames()


def _social_video_page(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or ""
    if (
        parsed.scheme == "https"
        and (host == "bilibili.com" or host.endswith(".bilibili.com"))
        and re.match(r"^/video/(?:BV|av)[A-Za-z0-9_-]+(?:/|$)", path, re.IGNORECASE)
    ):
        return "bilibili"
    if (
        parsed.scheme == "https"
        and (host == "douyin.com" or host.endswith(".douyin.com"))
        and re.match(r"^/video/[0-9]+(?:/|$)", path)
    ):
        return "douyin"
    return ""


def _subtitle_text(path: Path, max_chars: int = 12000) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    values: list[str] = []
    if path.suffix.lower() == ".json3":
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = {}
        for event in list(payload.get("events") or []) if isinstance(payload, dict) else []:
            if not isinstance(event, dict):
                continue
            text = "".join(
                str(segment.get("utf8") or "")
                for segment in list(event.get("segs") or [])
                if isinstance(segment, dict)
            ).strip()
            if text and text not in values:
                values.append(text)
    else:
        for line in raw.splitlines():
            text = re.sub(r"<[^>]+>", "", line).strip()
            if (
                not text
                or text.upper() in {"WEBVTT", "STYLE"}
                or "-->" in text
                or re.fullmatch(r"[0-9]+", text)
                or text.startswith(("NOTE", "Kind:", "Language:"))
            ):
                continue
            if text not in values:
                values.append(text)
    return " ".join(values)[:max_chars]


def _download_social_video_sync(
    url: str,
    temp_dir: Path,
    *,
    max_bytes: int,
    timeout: float,
) -> tuple[Path, str, str]:
    try:
        import yt_dlp
    except Exception as exc:  # pragma: no cover - dependency belongs to the host project
        raise RuntimeError("video_ytdlp_unavailable") from exc
    template = str(temp_dir / "source.%(ext)s")
    options = {
        "outtmpl": template,
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": max(8.0, float(timeout)),
        "max_filesize": int(max_bytes),
        "overwrites": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["zh-CN", "zh-Hans", "zh", "ai-zh"],
        "subtitlesformat": "vtt/json3/best",
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        prepared = Path(downloader.prepare_filename(info))
    candidates = [prepared, *sorted(temp_dir.glob("source.*"))]
    video_path = next(
        (
            path
            for path in candidates
            if path.exists() and path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
        ),
        None,
    )
    if video_path is None or video_path.stat().st_size <= 0 or video_path.stat().st_size > max_bytes:
        raise RuntimeError("video_ytdlp_download_failed")
    subtitle = ""
    for path in sorted(temp_dir.glob("source*")):
        if path.suffix.lower() in {".vtt", ".srt", ".json3"}:
            subtitle = _subtitle_text(path)
            if subtitle:
                break
    requested = info.get("requested_downloads") if isinstance(info, dict) else []
    requested_item = requested[0] if isinstance(requested, list) and requested and isinstance(requested[0], dict) else {}
    direct_url = str(requested_item.get("url") or info.get("url") or "").strip() if isinstance(info, dict) else ""
    if not direct_url.startswith("https://"):
        direct_url = ""
    return video_path, direct_url, subtitle


def _read_frames(path: Path, *, fps: float, width: int):
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return _system_ffmpeg_read_frames(path, fps=fps, width=width)

    vf = f"fps={fps:.6f},scale='min({int(width)},iw)':-2"
    return imageio_ffmpeg.read_frames(
        str(path),
        pix_fmt="rgb24",
        output_params=["-vf", vf, "-vsync", "vfr"],
    )


def _image_diff(left: Image.Image, right: Image.Image) -> float:
    diff = ImageChops.difference(left, right)
    histogram = diff.histogram()
    total = sum(index * count for index, count in enumerate(histogram))
    pixels = max(1, left.width * left.height)
    return min(1.0, total / (255.0 * pixels))


def _scan_video_sync(path: Path, *, scan_fps: float, max_samples: int) -> tuple[dict[str, Any], list[tuple[float, float]]]:
    reader = _read_frames(path, fps=scan_fps, width=320)
    metadata: dict[str, Any] = {}
    scores: list[tuple[float, float]] = []
    previous: Image.Image | None = None
    previous_bottom: Image.Image | None = None
    try:
        metadata = dict(next(reader) or {})
        size = tuple(metadata.get("size") or metadata.get("source_size") or (320, 180))
        width, height = max(2, int(size[0])), max(2, int(size[1]))
        for frame in reader:
            if len(scores) >= max_samples:
                break
            image = Image.frombytes("RGB", (width, height), frame).convert("L").resize((96, 54))
            bottom = image.crop((0, int(image.height * 0.62), image.width, image.height))
            scene = _image_diff(image, previous) if previous is not None else 1.0
            subtitle = _image_diff(bottom, previous_bottom) if previous_bottom is not None else 1.0
            scores.append((scene, subtitle))
            previous = image
            previous_bottom = bottom
    finally:
        reader.close()
    return metadata, scores


def _uniform_indices(total: int, count: int) -> list[int]:
    if total <= 0 or count <= 0:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [0]
    return [round(index * (total - 1) / (count - 1)) for index in range(count)]


def select_storyboard_frames(
    scores: Sequence[tuple[float, float]],
    *,
    target_frames: int,
    scan_fps: float,
) -> list[SelectedFrame]:
    total = len(scores)
    target = max(0, min(total, int(target_frames)))
    if target <= 0:
        return []
    uniform_count = max(2, int(round(target * 0.45)))
    scene_count = max(1, int(round(target * 0.35)))
    subtitle_count = max(1, target - uniform_count - scene_count)
    selected: set[int] = set(_uniform_indices(total, uniform_count))

    def add_ranked(position: int, count: int) -> None:
        ranked = sorted(range(total), key=lambda index: (-scores[index][position], index))
        for index in ranked:
            if index in selected:
                continue
            selected.add(index)
            if len(selected) >= min(target, uniform_count + count):
                break

    add_ranked(0, scene_count)
    before_subtitles = len(selected)
    ranked_subtitles = sorted(range(total), key=lambda index: (-scores[index][1], index))
    for index in ranked_subtitles:
        if index not in selected:
            selected.add(index)
        if len(selected) >= min(target, before_subtitles + subtitle_count):
            break
    if len(selected) < target:
        for index in _uniform_indices(total, target):
            selected.add(index)
            if len(selected) >= target:
                break
    if len(selected) < target:
        for index in range(total):
            selected.add(index)
            if len(selected) >= target:
                break
    return [
        SelectedFrame(
            index=index,
            timestamp_seconds=index / max(0.001, scan_fps),
            scene_score=float(scores[index][0]),
            subtitle_score=float(scores[index][1]),
        )
        for index in sorted(selected)[:target]
    ]


def _extract_selected_frames_sync(
    path: Path,
    *,
    scan_fps: float,
    selected: Sequence[SelectedFrame],
    output_dir: Path,
) -> list[Path]:
    wanted = {item.index: item for item in selected}
    if not wanted:
        return []
    reader = _read_frames(path, fps=scan_fps, width=960)
    paths: list[Path] = []
    try:
        metadata = dict(next(reader) or {})
        size = tuple(metadata.get("size") or (960, 540))
        width, height = max(2, int(size[0])), max(2, int(size[1]))
        for index, frame in enumerate(reader):
            if index not in wanted:
                if index > max(wanted):
                    break
                continue
            image = Image.frombytes("RGB", (width, height), frame)
            frame_path = output_dir / f"frame_{index:06d}.jpg"
            image.save(frame_path, format="JPEG", quality=82, optimize=True)
            paths.append(frame_path)
            if len(paths) >= len(wanted):
                break
    finally:
        reader.close()
    return paths


def _format_timestamp(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    return f"{total // 60:02d}:{total % 60:02d}"


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(values), max(1, size)):
        yield values[start : start + max(1, size)]


def _build_contact_sheets_sync(
    frame_paths: Sequence[Path],
    selected: Sequence[SelectedFrame],
    *,
    frames_per_sheet: int,
    output_dir: Path,
) -> list[Path]:
    timestamp_by_index = {item.index: item.timestamp_seconds for item in selected}
    sheets: list[Path] = []
    for sheet_index, group in enumerate(_chunks(list(frame_paths), frames_per_sheet), start=1):
        opened = [Image.open(path).convert("RGB") for path in group]
        try:
            portrait = sum(image.height > image.width for image in opened) > len(opened) / 2
            columns = 2 if portrait else 3
            rows = int(math.ceil(len(opened) / columns))
            cell_width = 360 if portrait else 384
            cell_height = 420 if portrait else 240
            canvas = Image.new("RGB", (columns * cell_width, rows * cell_height), "#101318")
            draw = ImageDraw.Draw(canvas)
            font = ImageFont.load_default()
            for index, (path, image) in enumerate(zip(group, opened)):
                thumb = image.copy()
                thumb.thumbnail((cell_width, cell_height - 24), Image.Resampling.LANCZOS)
                x = (index % columns) * cell_width + (cell_width - thumb.width) // 2
                y = (index // columns) * cell_height + 22 + (cell_height - 24 - thumb.height) // 2
                canvas.paste(thumb, (x, y))
                frame_index = int(path.stem.rsplit("_", 1)[-1])
                draw.rectangle((x, max(0, y - 20), x + 58, y), fill="#000000")
                draw.text((x + 4, max(1, y - 18)), _format_timestamp(timestamp_by_index.get(frame_index, 0.0)), fill="white", font=font)
            sheet_path = output_dir / f"storyboard_{sheet_index:03d}.jpg"
            canvas.save(sheet_path, format="JPEG", quality=80, optimize=True)
            sheets.append(sheet_path)
        finally:
            for image in opened:
                image.close()
    return sheets


def _refs_with_payload_budget(paths: Sequence[Path], max_bytes: int) -> tuple[list[str], list[str]]:
    refs: list[str] = []
    warnings: list[str] = []
    used = 0
    for path in paths:
        payload = path.read_bytes()
        encoded_size = int(math.ceil(len(payload) / 3) * 4)
        if refs and used + encoded_size > max_bytes:
            warnings.append("video_storyboard_payload_truncated")
            break
        refs.append(f"data:image/jpeg;base64,{base64.b64encode(payload).decode('ascii')}")
        used += encoded_size
    return refs, warnings


def _probe_video_sync(path: Path) -> dict[str, Any]:
    reader = _read_frames(path, fps=0.5, width=64)
    try:
        return dict(next(reader) or {})
    finally:
        reader.close()


def _extract_audio_sync(video_path: Path, audio_path: Path) -> bool:
    command = [
        _ffmpeg_executable(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(audio_path),
    ]
    completed = subprocess.run(command, capture_output=True, check=False, timeout=120)
    if completed.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size <= 44:
        audio_path.unlink(missing_ok=True)
        return False
    return True


async def _materialize_video(video_ref: str, temp_dir: Path, config: Any) -> tuple[Path, str, str]:
    normalized, problem = normalize_video_ref(video_ref)
    if not normalized:
        raise ValueError(f"invalid_video_ref:{problem or 'unknown'}")
    if not normalized.startswith(("http://", "https://")):
        return Path(normalized), "", ""
    suffix = Path(urlsplit(normalized).path).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}:
        suffix = ".mp4"
    target = temp_dir / f"source{suffix}"
    max_bytes = _bounded_int(
        getattr(config, "personification_video_max_bytes", _DEFAULT_MAX_VIDEO_BYTES),
        _DEFAULT_MAX_VIDEO_BYTES,
        8 * 1024 * 1024,
        512 * 1024 * 1024,
    )
    social_platform = _social_video_page(normalized)
    if social_platform:
        try:
            return await asyncio.to_thread(
                _download_social_video_sync,
                normalized,
                temp_dir,
                max_bytes=max_bytes,
                timeout=_bounded_float(
                    getattr(config, "personification_video_download_timeout", 90.0), 90.0, 8.0, 180.0
                ),
            )
        except Exception as exc:
            raise ValueError(f"video_{social_platform}_download_failed") from exc
    try:
        await download_public_media_to_path(
            normalized,
            target,
            timeout=_bounded_float(getattr(config, "personification_video_download_timeout", 90.0), 90.0, 8.0, 180.0),
            max_bytes=max_bytes,
            allowed_mimes=_VIDEO_MIMES,
        )
    except SafeMediaDownloadError as exc:
        raise ValueError("video_download_failed") from exc
    return target, normalized, ""


async def prepare_video_storyboard(video_ref: str, config: Any) -> VideoStoryboard:
    temp_dir = Path(tempfile.mkdtemp(prefix="personification-video-"))
    try:
        video_path, source_url, subtitle_text = await _materialize_video(video_ref, temp_dir, config)
        try:
            probe = await asyncio.to_thread(_probe_video_sync, video_path)
        except RuntimeError as exc:
            if str(exc or "").startswith(("video_ffmpeg_unavailable", "video_ffprobe_unavailable")):
                raise
            raise RuntimeError("video_storyboard_probe_failed") from exc
        except Exception as exc:
            raise RuntimeError("video_storyboard_probe_failed") from exc
        duration = float(probe.get("duration") or 0.0)
        source_fps = float(probe.get("fps") or 0.0)
        source_size_raw = tuple(probe.get("source_size") or probe.get("size") or (0, 0))
        source_size = (int(source_size_raw[0]), int(source_size_raw[1]))
        budget = resolve_video_frame_budget(duration, config)
        try:
            metadata, scores = await asyncio.to_thread(
                _scan_video_sync,
                video_path,
                scan_fps=budget.scan_fps,
                max_samples=budget.max_scan_samples,
            )
        except RuntimeError as exc:
            if str(exc or "").startswith(("video_ffmpeg_unavailable", "video_ffprobe_unavailable")):
                raise
            raise RuntimeError("video_storyboard_scan_failed") from exc
        except Exception as exc:
            raise RuntimeError("video_storyboard_scan_failed") from exc
        if not duration:
            duration = float(metadata.get("duration") or (len(scores) / max(0.001, budget.scan_fps)))
        selected = select_storyboard_frames(
            scores,
            target_frames=min(budget.target_frames, len(scores)),
            scan_fps=budget.scan_fps,
        )
        frames_dir = temp_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        try:
            frame_paths = await asyncio.to_thread(
                _extract_selected_frames_sync,
                video_path,
                scan_fps=budget.scan_fps,
                selected=selected,
                output_dir=frames_dir,
            )
        except RuntimeError as exc:
            if str(exc or "").startswith(("video_ffmpeg_unavailable", "video_ffprobe_unavailable")):
                raise
            raise RuntimeError("video_storyboard_frame_extract_failed") from exc
        except Exception as exc:
            raise RuntimeError("video_storyboard_frame_extract_failed") from exc
        sheets_dir = temp_dir / "sheets"
        sheets_dir.mkdir(parents=True, exist_ok=True)
        try:
            sheets = await asyncio.to_thread(
                _build_contact_sheets_sync,
                frame_paths,
                selected,
                frames_per_sheet=budget.contact_sheet_frames,
                output_dir=sheets_dir,
            )
        except Exception as exc:
            raise RuntimeError("video_storyboard_sheet_build_failed") from exc
        max_payload = _bounded_int(
            getattr(config, "personification_video_payload_max_bytes", _DEFAULT_PAYLOAD_BYTES),
            _DEFAULT_PAYLOAD_BYTES,
            1024 * 1024,
            32 * 1024 * 1024,
        )
        refs, warnings = await asyncio.to_thread(_refs_with_payload_budget, sheets, max_payload)
        audio_path = temp_dir / "audio.wav"
        has_audio = await asyncio.to_thread(_extract_audio_sync, video_path, audio_path)
        return VideoStoryboard(
            source_ref=str(video_ref or ""),
            source_url=source_url,
            video_path=video_path,
            temp_dir=temp_dir,
            duration_seconds=duration,
            source_fps=source_fps,
            scan_fps=budget.scan_fps,
            source_size=source_size,
            target_frame_count=budget.target_frames,
            selected_frames=selected,
            frame_paths=frame_paths,
            contact_sheet_paths=sheets,
            contact_sheet_refs=refs,
            audio_path=audio_path if has_audio else None,
            subtitle_text=subtitle_text,
            warnings=warnings + ([] if has_audio else ["video_audio_unavailable"]),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


__all__ = [
    "SelectedFrame",
    "VideoFrameBudget",
    "VideoStoryboard",
    "normalize_video_frame_preset",
    "prepare_video_storyboard",
    "resolve_video_frame_budget",
    "select_storyboard_frames",
]
