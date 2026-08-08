from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from ._loader import load_personification_module


audio_transcription = load_personification_module("plugin.personification.core.audio_transcription")


def test_audio_presets_use_requested_dashscope_models() -> None:
    qwen = audio_transcription.resolve_transcription_settings(
        SimpleNamespace(
            personification_audio_transcription_enabled=True,
            personification_audio_transcription_provider="auto",
            personification_audio_transcription_api_key="key",
        )
    )
    economy = audio_transcription.resolve_transcription_settings(
        SimpleNamespace(
            personification_audio_transcription_enabled=True,
            personification_audio_transcription_provider="paraformer",
            personification_audio_transcription_api_key="key",
        )
    )

    assert qwen["provider"] == "qwen_audio"
    assert qwen["model"] == "qwen-audio-3.0-asr-flash-filetrans"
    assert qwen["protocol"] == "dashscope_async_url"
    assert economy["model"] == "paraformer-v2"


def test_workspace_builds_current_bailian_endpoint() -> None:
    settings = audio_transcription.resolve_transcription_settings(
        SimpleNamespace(
            personification_audio_transcription_enabled=True,
            personification_audio_transcription_provider="qwen_audio",
            personification_audio_transcription_api_key="key",
            personification_audio_transcription_workspace_id="ws-123",
        )
    )
    assert settings["endpoint"] == (
        "https://ws-123.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription"
    )


def test_dashscope_url_mode_requires_public_source_url(tmp_path: Path) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF" + b"0" * 100)
    result = asyncio.run(
        audio_transcription.transcribe_audio_file(
            audio,
            SimpleNamespace(
                personification_audio_transcription_enabled=True,
                personification_audio_transcription_provider="qwen_audio",
                personification_audio_transcription_api_key="key",
            ),
        )
    )
    assert result.status == "unavailable"
    assert result.error_code == "audio_transcription_public_url_required"


def test_qwen_async_submission_context_hotwords_and_result(monkeypatch) -> None:  # noqa: ANN001
    requests: list[tuple[str, str, dict]] = []

    class _Response:
        def __init__(self, payload):  # noqa: ANN001
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):  # noqa: ANN201
            return self._payload

    class _Client:
        def __init__(self, **_kwargs):  # noqa: ANN001
            self.polls = 0

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, url, headers=None, json=None, **_kwargs):  # noqa: ANN001, ANN201
            requests.append(("POST", url, {"headers": headers or {}, "json": json or {}}))
            return _Response({"output": {"task_status": "PENDING", "task_id": "task-1"}})

        async def get(self, url, headers=None):  # noqa: ANN001, ANN201
            requests.append(("GET", url, {"headers": headers or {}}))
            if "/api/v1/tasks/" in url:
                return _Response(
                    {
                        "output": {
                            "task_status": "SUCCEEDED",
                            "results": [
                                {
                                    "subtask_status": "SUCCEEDED",
                                    "transcription_url": "https://result.example/transcript.json",
                                }
                            ],
                        }
                    }
                )
            return _Response(
                {
                    "transcripts": [
                        {
                            "transcript": "红狼使用 QCQ171 修脚撤离",
                            "sentences": [{"text": "红狼使用 QCQ171", "speaker_id": 0}],
                        }
                    ]
                }
            )

    monkeypatch.setattr(audio_transcription.httpx, "AsyncClient", _Client)
    result = asyncio.run(
        audio_transcription.transcribe_audio_file(
            None,
            SimpleNamespace(
                personification_audio_transcription_enabled=True,
                personification_audio_transcription_provider="qwen_audio",
                personification_audio_transcription_api_key="key",
                personification_audio_transcription_hotwords=["红狼"],
                personification_audio_transcription_diarization_enabled=True,
                personification_audio_transcription_speaker_count=2,
                personification_audio_transcription_poll_seconds=0.5,
            ),
            source_url="https://cdn.example/video.mp4",
            context_terms=["三角洲行动", "QCQ171", "花来"],
        )
    )

    assert result.available is True
    assert result.text == "红狼使用 QCQ171 修脚撤离"
    body = requests[0][2]["json"]
    assert body["model"] == "qwen-audio-3.0-asr-flash-filetrans"
    assert body["input"]["context"][0]["content"][0]["text"] == "三角洲行动；QCQ171；花来"
    assert body["parameters"]["vocabulary"] == {"红狼": 5, "三角洲行动": 5, "QCQ171": 5, "花来": 5}
    assert body["parameters"]["diarization_enabled"] is True
    assert body["parameters"]["speaker_count"] == 2


def test_paraformer_does_not_send_qwen_only_context_or_instant_hotwords(monkeypatch) -> None:  # noqa: ANN001
    captured: dict = {}

    class _Response:
        def __init__(self, payload):  # noqa: ANN001
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):  # noqa: ANN201
            return self.payload

    class _Client:
        def __init__(self, **_kwargs):  # noqa: ANN001
            pass

        async def __aenter__(self):  # noqa: ANN201
            return self

        async def __aexit__(self, *_args):  # noqa: ANN001, ANN201
            return None

        async def post(self, _url, json=None, **_kwargs):  # noqa: ANN001, ANN201
            captured.update(json or {})
            return _Response({"output": {"task_id": "task-2"}})

        async def get(self, url, **_kwargs):  # noqa: ANN001, ANN201
            if "/tasks/" in url:
                return _Response(
                    {"output": {"task_status": "SUCCEEDED", "results": [{"subtask_status": "SUCCEEDED", "transcription_url": "https://result.example/r.json"}]}}
                )
            return _Response({"transcripts": [{"transcript": "普通中文"}]})

    monkeypatch.setattr(audio_transcription.httpx, "AsyncClient", _Client)
    result = asyncio.run(
        audio_transcription.transcribe_audio_file(
            None,
            SimpleNamespace(
                personification_audio_transcription_enabled=True,
                personification_audio_transcription_provider="paraformer",
                personification_audio_transcription_api_key="key",
                personification_audio_transcription_hotwords=["花来"],
            ),
            source_url="https://cdn.example/audio.wav",
            context_terms=["三角洲行动"],
        )
    )
    assert result.available is True
    assert "context" not in captured["input"]
    assert "vocabulary" not in captured["parameters"]
