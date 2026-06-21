from __future__ import annotations

import asyncio
from typing import Any

from ._loader import load_personification_module


weather_impl = load_personification_module("plugin.personification.skills.skillpacks.weather.scripts.impl")


class _FakeResponse:
    def __init__(self, *, text: str = "", json_data: Any = None) -> None:
        self.text = text
        self._json_data = json_data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._json_data


def _install_fake_client(monkeypatch, responses: list[_FakeResponse], calls: list[dict[str, Any]]) -> None:  # noqa: ANN001
    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            calls.append({"url": url, **kwargs})
            if not responses:
                raise AssertionError("unexpected http request")
            return responses.pop(0)

    monkeypatch.setattr(weather_impl.httpx, "AsyncClient", _FakeAsyncClient)


def test_fetch_weather_days_one_keeps_realtime_wttr_path(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    responses = [_FakeResponse(text="广州: 小雨 +28°C")]
    _install_fake_client(monkeypatch, responses, calls)

    result = asyncio.run(weather_impl.fetch_weather("广州", days=1))

    assert result == "广州: 小雨 +28°C"
    assert len(calls) == 1
    assert "wttr.in" in calls[0]["url"]


def test_fetch_weather_multi_day_returns_plain_forecast_summary(monkeypatch) -> None:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    days = [f"2026-06-{day:02d}" for day in range(20, 35)]
    responses = [
        _FakeResponse(json_data=[{"name": "广州", "lat": 23.13, "lon": 113.26}]),
        _FakeResponse(
            json_data={
                "daily": {
                    "time": days,
                    "weather_code": [3, 61, 63, 65, 80, 3, 3, 95, 61, 3, 3, 63, 3, 3, 61],
                    "temperature_2m_max": [31, 30, 29, 28, 30, 32, 33, 29, 30, 31, 32, 30, 31, 32, 33],
                    "temperature_2m_min": [25, 24, 24, 23, 24, 25, 25, 23, 24, 24, 25, 24, 24, 25, 26],
                    "precipitation_sum": [0, 2, 6, 12, 8, 0, 0, 18, 1, 0, 0, 5, 0, 0, 1],
                    "precipitation_probability_max": [20, 55, 70, 90, 75, 10, 15, 95, 50, 20, 20, 65, 10, 10, 55],
                }
            }
        ),
    ]
    _install_fake_client(monkeypatch, responses, calls)

    result = asyncio.run(weather_impl.fetch_weather("广州", days=15))

    assert "广州未来15天" in result
    assert "有雨大概" in result
    assert "6月21日" in result
    assert "气温约23-33度" in result
    assert "- " not in result
    assert "**" not in result
    assert len(calls) == 2
    assert calls[1]["params"]["forecast_days"] == 15


def test_coerce_forecast_days_clamps_to_supported_range() -> None:
    assert weather_impl.coerce_forecast_days(0) == 1
    assert weather_impl.coerce_forecast_days("15") == 15
    assert weather_impl.coerce_forecast_days(30) == 16
