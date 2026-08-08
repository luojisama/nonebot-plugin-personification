from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._loader import load_personification_module


def test_mimo_helper_exposes_no_agent_tools_without_launching_browser(tmp_path: Path) -> None:
    compat = load_personification_module("plugin.personification.skill_runtime.mcp_compat")
    project_root = Path(__file__).resolve().parents[2]
    entrypoint = Path(__file__).resolve().parents[1] / "core" / "mimo_web_asr_entrypoint.py"

    async def run() -> tuple[list[dict], dict]:
        async with compat.McpStdioClient(
            command=sys.executable,
            args=[str(entrypoint)],
            env={**os.environ, "PERSONIFICATION_MIMO_WEB_ASR_ROOT": str(tmp_path / "mimo")},
            cwd=str(project_root),
            timeout=8,
        ) as client:
            return await client.list_tools(), await client.request("personification/mimo-web-asr/status", {})

    tools, status = asyncio.run(run())
    assert tools == []
    assert status["state"] == "login_required"
    assert status["browser_running"] is False
    assert status["page_contract_version"] == "mimo_studio_asr_v1"


def test_mimo_service_disabled_status_does_not_start_helper(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    module = load_personification_module("plugin.personification.core.mimo_web_asr_service")
    service = module.MiMoWebAsrService(tmp_path)
    config = SimpleNamespace(
        personification_mimo_web_asr_enabled=False,
        personification_mimo_web_asr_risk_acknowledged=False,
    )

    async def forbidden(_config):  # noqa: ANN001, ANN202
        raise AssertionError("disabled status must not start helper")

    monkeypatch.setattr(service, "_ensure_client", forbidden)
    status = asyncio.run(service.status(config, refresh=True))
    assert status["state"] == "disabled"
    assert status["last_diagnostic_code"] == "mimo_web_asr_disabled"


def test_mimo_runtime_rejects_paths_instead_of_registered_tokens(tmp_path: Path) -> None:
    module = load_personification_module("plugin.personification.core.mimo_web_asr_runtime")
    runtime = module.MiMoWebAsrRuntime(tmp_path)
    with pytest.raises(ValueError, match="mimo_web_asr_media_token_invalid"):
        runtime._resolve_media_token(str(tmp_path / "voice.wav"))


def test_mimo_runtime_detects_network_risk_without_clicking(tmp_path: Path) -> None:
    module = load_personification_module("plugin.personification.core.mimo_web_asr_runtime")

    class _Body:
        first = None

        def __init__(self) -> None:
            self.first = self

        async def inner_text(self, timeout: int = 0) -> str:
            del timeout
            return "当前网络环境存在风险，请稍后再试"

        async def is_visible(self, timeout: int = 0) -> bool:
            del timeout
            return False

    class _Page:
        def locator(self, _selector: str) -> _Body:
            return _Body()

    runtime = module.MiMoWebAsrRuntime(tmp_path)
    state, code = asyncio.run(runtime._page_state(_Page()))
    assert state == "manual_verification_required"
    assert code == "mimo_web_asr_network_risk_detected"
    assert runtime._risk_blocked_until > 0


def test_mimo_runtime_selects_asr_model_from_public_model_menu(tmp_path: Path) -> None:
    module = load_personification_module("plugin.personification.core.mimo_web_asr_runtime")
    clicks: list[str] = []

    class _Locator:
        def __init__(self, page: "_Page", name: str) -> None:
            self.page = page
            self.name = name
            self.first = self

        async def is_visible(self, timeout: int = 0) -> bool:
            del timeout
            return self.name == "menu" or (self.name == "asr" and self.page.opened)

        async def click(self, timeout: int = 0) -> None:
            del timeout
            clicks.append(self.name)
            if self.name == "menu":
                self.page.opened = True

    class _Page:
        opened = False

        def locator(self, selector: str) -> _Locator:
            if selector == 'button[aria-label*="模型"]':
                return _Locator(self, "menu")
            if selector == '[role="option"]:has-text("MiMo-V2.5-ASR")':
                return _Locator(self, "asr")
            return _Locator(self, "missing")

        async def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

    runtime = module.MiMoWebAsrRuntime(tmp_path)
    asyncio.run(runtime._select_asr_model(_Page()))
    assert clicks == ["menu", "asr"]


def test_consumer_web_coordinator_allows_one_active_and_one_waiter() -> None:
    module = load_personification_module("plugin.personification.core.consumer_web_coordinator")
    coordinator = module.ConsumerWebCoordinator()
    closed: list[str] = []

    async def close_gemini() -> None:
        closed.append("gemini")

    async def close_mimo() -> None:
        closed.append("mimo")

    coordinator.register("gemini", close=close_gemini, protected=lambda: False)
    coordinator.register("mimo_asr", close=close_mimo, protected=lambda: False)

    async def run() -> None:
        release = asyncio.Event()
        entered = asyncio.Event()

        async def first() -> None:
            async with coordinator.admit("gemini"):
                entered.set()
                await release.wait()

        async def second() -> None:
            async with coordinator.admit("mimo_asr"):
                return None

        first_task = asyncio.create_task(first())
        await entered.wait()
        second_task = asyncio.create_task(second())
        for _ in range(30):
            if coordinator.snapshot("mimo_asr")["waiting"] == 1:
                break
            await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="consumer_web_busy"):
            async with coordinator.admit("gemini"):
                pass
        release.set()
        await asyncio.gather(first_task, second_task)

    asyncio.run(run())
    assert closed == ["gemini"]


def test_consumer_web_switch_is_blocked_by_interactive_session_without_leaking_slot() -> None:
    module = load_personification_module("plugin.personification.core.consumer_web_coordinator")
    coordinator = module.ConsumerWebCoordinator()
    protected = {"gemini": True}

    async def close() -> None:
        return None

    coordinator.register("gemini", close=close, protected=lambda: protected["gemini"])
    coordinator.register("mimo_asr", close=close, protected=lambda: False)

    async def run() -> None:
        await coordinator.activate("gemini")
        with pytest.raises(RuntimeError, match="consumer_web_busy"):
            async with coordinator.admit("mimo_asr"):
                pass
        assert coordinator.snapshot("mimo_asr")["active"] is False
        assert coordinator.snapshot("mimo_asr")["waiting"] == 0
        protected["gemini"] = False
        async with coordinator.admit("mimo_asr"):
            assert coordinator.snapshot("mimo_asr")["active"] is True

    asyncio.run(run())
