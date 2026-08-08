from __future__ import annotations

import asyncio
import base64
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx


_DASHSCOPE_SUBMIT_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
_QWEN_AUDIO_MODEL = "qwen-audio-3.0-asr-flash-filetrans"
_PARAFORMER_MODEL = "paraformer-v2"
_OPENAI_DEFAULT_MODEL = "gpt-4o-mini-transcribe"
_DEFAULT_TIMEOUT_SECONDS = 180.0
_MAX_TIMEOUT_SECONDS = 600.0
_DEFAULT_POLL_SECONDS = 1.5
_DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024
_HARD_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_DEFAULT_MAX_TRANSCRIPT_CHARS = 12000
_HARD_MAX_TRANSCRIPT_CHARS = 50000
_TERMINAL_TASK_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED", "UNKNOWN"}


@dataclass(frozen=True)
class AudioTranscriptResult:
    text: str = ""
    provider: str = ""
    model: str = ""
    status: str = "disabled"
    language: str = ""
    confidence: float | None = None
    segments: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    error_code: str = ""
    task_id: str = ""

    @property
    def available(self) -> bool:
        return bool(self.text.strip()) and self.status == "ready"


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bounded_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def normalize_transcription_provider(value: Any) -> str:
    normalized = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "off": "disabled",
        "none": "disabled",
        "qwen": "qwen_audio",
        "qwen_audio_3": "qwen_audio",
        "qwen_audio_3_0_asr_flash_filetrans": "qwen_audio",
        "paraformer_v2": "paraformer",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in {"auto", "qwen_audio", "paraformer", "custom", "disabled"} else "custom"


def normalize_custom_transcription_protocol(value: Any) -> str:
    normalized = str(value or "dashscope_async_url").strip().lower().replace("-", "_")
    aliases = {
        "dashscope": "dashscope_async_url",
        "dashscope_async": "dashscope_async_url",
        "openai": "openai_multipart",
        "multipart": "openai_multipart",
        "json": "json_base64",
        "base64": "json_base64",
    }
    normalized = aliases.get(normalized, normalized)
    supported = {"dashscope_async_url", "openai_multipart", "json_base64"}
    return normalized if normalized in supported else "dashscope_async_url"


def _validate_endpoint(value: str, *, allow_http_local: bool = True) -> str:
    endpoint = str(value or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("audio_transcription_endpoint_invalid")
    if parsed.scheme == "http" and (
        not allow_http_local or parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("audio_transcription_endpoint_insecure")
    return endpoint


def _dashscope_endpoint(workspace_id: str, configured_url: str) -> str:
    if configured_url:
        return configured_url
    workspace = str(workspace_id or "").strip()
    if workspace:
        return f"https://{workspace}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription"
    return _DASHSCOPE_SUBMIT_URL


def resolve_transcription_settings(config: Any) -> dict[str, Any]:
    enabled = bool(getattr(config, "personification_audio_transcription_enabled", True))
    provider = normalize_transcription_provider(
        getattr(config, "personification_audio_transcription_provider", "auto")
    )
    api_key = str(getattr(config, "personification_audio_transcription_api_key", "") or "").strip()
    configured_url = str(getattr(config, "personification_audio_transcription_api_url", "") or "").strip()
    configured_model = str(getattr(config, "personification_audio_transcription_model", "") or "").strip()
    workspace_id = str(getattr(config, "personification_audio_transcription_workspace_id", "") or "").strip()
    custom_protocol = normalize_custom_transcription_protocol(
        getattr(config, "personification_audio_transcription_custom_protocol", "dashscope_async_url")
    )

    if not enabled or provider == "disabled":
        provider = "disabled"
    elif provider == "auto":
        provider = "qwen_audio" if api_key else "disabled"

    if provider == "qwen_audio":
        endpoint = _dashscope_endpoint(workspace_id, configured_url)
        model = configured_model or _QWEN_AUDIO_MODEL
        protocol = "dashscope_async_url"
    elif provider == "paraformer":
        endpoint = _dashscope_endpoint(workspace_id, configured_url)
        model = configured_model or _PARAFORMER_MODEL
        protocol = "dashscope_async_url"
    elif provider == "custom":
        endpoint = configured_url
        model = configured_model
        protocol = custom_protocol
    else:
        endpoint = ""
        model = configured_model
        protocol = custom_protocol

    hotwords = getattr(config, "personification_audio_transcription_hotwords", []) or []
    if isinstance(hotwords, str):
        hotwords = [item.strip() for item in hotwords.replace("，", ",").split(",") if item.strip()]
    return {
        "enabled": provider != "disabled",
        "provider": provider,
        "protocol": protocol,
        "endpoint": endpoint,
        "api_key": api_key,
        "workspace_id": workspace_id,
        "model": model,
        "language": str(getattr(config, "personification_audio_transcription_language", "auto") or "").strip(),
        "prompt": str(getattr(config, "personification_audio_transcription_prompt", "") or "").strip()[:1000],
        "hotwords": [str(item).strip()[:64] for item in list(hotwords) if str(item).strip()][:100],
        "diarization_enabled": bool(
            getattr(config, "personification_audio_transcription_diarization_enabled", False)
        ),
        "speaker_count": _bounded_int(
            getattr(config, "personification_audio_transcription_speaker_count", 0),
            0,
            minimum=0,
            maximum=24,
        ),
        "timeout": _bounded_float(
            getattr(config, "personification_audio_transcription_timeout", _DEFAULT_TIMEOUT_SECONDS),
            _DEFAULT_TIMEOUT_SECONDS,
            minimum=15.0,
            maximum=_MAX_TIMEOUT_SECONDS,
        ),
        "poll_seconds": _bounded_float(
            getattr(config, "personification_audio_transcription_poll_seconds", _DEFAULT_POLL_SECONDS),
            _DEFAULT_POLL_SECONDS,
            minimum=0.5,
            maximum=10.0,
        ),
        "max_audio_bytes": _bounded_int(
            getattr(config, "personification_audio_transcription_max_bytes", _DEFAULT_MAX_AUDIO_BYTES),
            _DEFAULT_MAX_AUDIO_BYTES,
            minimum=65536,
            maximum=_HARD_MAX_AUDIO_BYTES,
        ),
        "max_transcript_chars": _bounded_int(
            getattr(config, "personification_audio_transcription_max_chars", _DEFAULT_MAX_TRANSCRIPT_CHARS),
            _DEFAULT_MAX_TRANSCRIPT_CHARS,
            minimum=500,
            maximum=_HARD_MAX_TRANSCRIPT_CHARS,
        ),
    }


def _mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "audio/wav"


def _safe_confidence(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def _extract_openai_result(payload: Any) -> tuple[str, list[dict[str, Any]], float | None]:
    if isinstance(payload, str):
        return payload.strip(), [], None
    if not isinstance(payload, dict):
        return "", [], None
    text = str(payload.get("text") or payload.get("transcript") or "").strip()
    segments = [dict(item) for item in list(payload.get("segments") or []) if isinstance(item, dict)]
    return text, segments[:500], _safe_confidence(payload.get("confidence"))


def _extract_dashscope_result(payload: Any) -> tuple[str, list[dict[str, Any]], float | None]:
    if not isinstance(payload, dict):
        return "", [], None
    transcripts = payload.get("transcripts")
    roots = list(transcripts) if isinstance(transcripts, list) else [payload]
    texts: list[str] = []
    segments: list[dict[str, Any]] = []
    for root in roots:
        if not isinstance(root, dict):
            continue
        transcript = str(root.get("transcript") or root.get("text") or "").strip()
        if transcript and transcript not in texts:
            texts.append(transcript)
        sentences = root.get("sentences")
        if isinstance(sentences, list):
            for sentence in sentences:
                if not isinstance(sentence, dict):
                    continue
                segments.append(dict(sentence))
                sentence_text = str(sentence.get("text") or "").strip()
                if sentence_text and not transcript:
                    texts.append(sentence_text)
    return " ".join(texts).strip(), segments[:500], None


def _task_endpoint(submit_endpoint: str, task_id: str) -> str:
    root = submit_endpoint.split("/api/v1/", 1)[0]
    return f"{root}/api/v1/tasks/{task_id}"


def _dashscope_context(prompt: str, terms: Sequence[str]) -> list[dict[str, Any]]:
    values: list[str] = []
    if str(prompt or "").strip():
        values.append(str(prompt).strip())
    for term in terms:
        value = str(term or "").strip()
        if value and value not in values:
            values.append(value)
    if not values:
        return []
    text = "；".join(values)[:400]
    return [{"role": "user", "content": [{"type": "input_text", "text": text}]}]


def _dashscope_hotwords(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in values:
        value = str(item or "").strip()[:64]
        if value and value not in result:
            result[value] = 5
        if len(result) >= 100:
            break
    return result


async def _call_dashscope_async(
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    model: str,
    source_url: str,
    prompt: str,
    context_terms: Sequence[str],
    hotwords: Sequence[str],
    diarization_enabled: bool,
    speaker_count: int,
    poll_seconds: float,
    deadline: float,
) -> tuple[str, list[dict[str, Any]], float | None, str]:
    validated_source = _validate_endpoint(source_url, allow_http_local=False)
    input_payload: dict[str, Any] = {"file_urls": [validated_source]}
    context = _dashscope_context(prompt, context_terms)
    if context and model == _QWEN_AUDIO_MODEL:
        input_payload["context"] = context
    parameters: dict[str, Any] = {"channel_id": [0]}
    if model == _QWEN_AUDIO_MODEL:
        vocabulary = _dashscope_hotwords([*hotwords, *context_terms])
        if vocabulary:
            parameters["vocabulary"] = vocabulary
    if diarization_enabled:
        parameters["diarization_enabled"] = True
        if speaker_count > 0:
            parameters["speaker_count"] = speaker_count
    response = await client.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        },
        json={"model": model, "input": input_payload, "parameters": parameters},
    )
    response.raise_for_status()
    submit = dict(response.json() or {})
    output = submit.get("output") if isinstance(submit.get("output"), dict) else {}
    task_id = str(output.get("task_id") or submit.get("task_id") or "").strip()
    if not task_id:
        raise ValueError("audio_transcription_task_missing")
    task_url = _task_endpoint(endpoint, task_id)
    task_payload: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        poll = await client.get(task_url, headers={"Authorization": f"Bearer {api_key}"})
        poll.raise_for_status()
        task_payload = dict(poll.json() or {})
        task_output = task_payload.get("output") if isinstance(task_payload.get("output"), dict) else task_payload
        status = str(task_output.get("task_status") or "").strip().upper()
        if status in _TERMINAL_TASK_STATES:
            if status != "SUCCEEDED":
                raise ValueError("audio_transcription_task_failed")
            results = [item for item in list(task_output.get("results") or []) if isinstance(item, dict)]
            succeeded = [item for item in results if str(item.get("subtask_status") or "").upper() == "SUCCEEDED"]
            result = succeeded[0] if succeeded else (results[0] if results else {})
            result_url = str(result.get("transcription_url") or "").strip()
            if not result_url:
                raise ValueError("audio_transcription_result_url_missing")
            result_response = await client.get(_validate_endpoint(result_url, allow_http_local=False))
            result_response.raise_for_status()
            text, segments, confidence = _extract_dashscope_result(result_response.json())
            return text, segments, confidence, task_id
        await asyncio.sleep(poll_seconds)
    raise TimeoutError("audio_transcription_task_timeout")


async def transcribe_audio_file(
    path: str | Path | None,
    config: Any,
    *,
    source_url: str = "",
    context_terms: Sequence[str] = (),
) -> AudioTranscriptResult:
    settings = resolve_transcription_settings(config)
    provider = str(settings["provider"])
    model = str(settings["model"])
    if not settings["enabled"]:
        return AudioTranscriptResult(provider=provider, model=model, status="disabled")
    if not settings["api_key"]:
        return AudioTranscriptResult(
            provider=provider,
            model=model,
            status="unavailable",
            error_code="audio_transcription_api_key_missing",
        )
    audio_path = Path(path) if path else None
    protocol = str(settings["protocol"])
    if protocol != "dashscope_async_url":
        try:
            size = audio_path.stat().st_size if audio_path else 0
        except OSError:
            size = 0
        if size <= 0:
            return AudioTranscriptResult(
                provider=provider, model=model, status="unavailable", error_code="audio_file_unavailable"
            )
        if size > int(settings["max_audio_bytes"]):
            return AudioTranscriptResult(
                provider=provider, model=model, status="unavailable", error_code="audio_file_too_large"
            )
    elif not str(source_url or "").strip():
        return AudioTranscriptResult(
            provider=provider,
            model=model,
            status="unavailable",
            error_code="audio_transcription_public_url_required",
        )
    try:
        endpoint = _validate_endpoint(str(settings["endpoint"]))
    except ValueError as exc:
        return AudioTranscriptResult(provider=provider, model=model, status="unavailable", error_code=str(exc))

    task_id = ""
    try:
        timeout_seconds = float(settings["timeout"])
        timeout = httpx.Timeout(timeout_seconds, connect=min(15.0, timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            if protocol == "dashscope_async_url":
                text, segments, confidence, task_id = await _call_dashscope_async(
                    client=client,
                    endpoint=endpoint,
                    api_key=str(settings["api_key"]),
                    model=model or _QWEN_AUDIO_MODEL,
                    source_url=str(source_url or ""),
                    prompt=str(settings["prompt"]),
                    context_terms=context_terms,
                    hotwords=list(settings["hotwords"]),
                    diarization_enabled=bool(settings["diarization_enabled"]),
                    speaker_count=int(settings["speaker_count"]),
                    poll_seconds=float(settings["poll_seconds"]),
                    deadline=asyncio.get_running_loop().time() + timeout_seconds,
                )
            else:
                assert audio_path is not None
                audio = await asyncio.to_thread(audio_path.read_bytes)
                if protocol == "openai_multipart":
                    form: dict[str, str] = {"model": model or _OPENAI_DEFAULT_MODEL, "response_format": "json"}
                    language = str(settings["language"] or "").strip()
                    if language.lower() not in {"", "auto", "multi", "multilingual"}:
                        form["language"] = language[:32]
                    prompt = "；".join(
                        item for item in [str(settings["prompt"]), *[str(v) for v in context_terms]] if item.strip()
                    )
                    if prompt:
                        form["prompt"] = prompt[:1000]
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {settings['api_key']}"},
                        data=form,
                        files={"file": (audio_path.name, audio, _mime_type(audio_path))},
                    )
                    response.raise_for_status()
                    text, segments, confidence = _extract_openai_result(response.json())
                else:
                    body: dict[str, Any] = {
                        "model": model,
                        "audio": {"data": base64.b64encode(audio).decode("ascii"), "mime_type": _mime_type(audio_path)},
                        "context": [str(item) for item in context_terms if str(item).strip()][:20],
                    }
                    response = await client.post(
                        endpoint,
                        headers={"Authorization": f"Bearer {settings['api_key']}"},
                        json=body,
                    )
                    response.raise_for_status()
                    text, segments, confidence = _extract_openai_result(response.json())
    except TimeoutError:
        return AudioTranscriptResult(
            provider=provider, model=model, status="failed", error_code="audio_transcription_task_timeout", task_id=task_id
        )
    except (httpx.HTTPError, ValueError, OSError):
        return AudioTranscriptResult(
            provider=provider, model=model, status="failed", error_code="audio_transcription_request_failed", task_id=task_id
        )

    normalized = " ".join(str(text or "").split())[: int(settings["max_transcript_chars"])]
    if not normalized:
        return AudioTranscriptResult(
            provider=provider,
            model=model,
            status="empty",
            language=str(settings["language"]),
            error_code="audio_transcription_empty",
            task_id=task_id,
        )
    return AudioTranscriptResult(
        text=normalized,
        provider=provider,
        model=model,
        status="ready",
        language=str(settings["language"]),
        confidence=confidence,
        segments=tuple(segments),
        task_id=task_id,
    )


__all__ = [
    "AudioTranscriptResult",
    "normalize_custom_transcription_protocol",
    "normalize_transcription_provider",
    "resolve_transcription_settings",
    "transcribe_audio_file",
]
