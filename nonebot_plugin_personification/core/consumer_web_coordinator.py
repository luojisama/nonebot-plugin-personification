from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Awaitable, Callable


CloseCallback = Callable[[], Awaitable[None]]
ProtectedCallback = Callable[[], bool]


@dataclass(frozen=True)
class _Participant:
    close: CloseCallback
    protected: ProtectedCallback


class ConsumerWebCoordinator:
    """Keep consumer-web automation within one process and one Chromium slot."""

    def __init__(self) -> None:
        self._participants: dict[str, _Participant] = {}
        self._switch_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._admission = asyncio.Semaphore(1)
        self._current = ""
        self._active_service = ""
        self._waiting_by_service: dict[str, int] = {}

    def register(
        self,
        service: str,
        *,
        close: CloseCallback,
        protected: ProtectedCallback,
    ) -> None:
        name = str(service or "").strip()
        if not name:
            raise ValueError("consumer_web_service_invalid")
        self._participants[name] = _Participant(close=close, protected=protected)

    async def activate(self, service: str) -> None:
        name = str(service or "").strip()
        if name not in self._participants:
            raise RuntimeError("consumer_web_service_invalid")
        async with self._switch_lock:
            current = self._current
            if current == name:
                return
            participant = self._participants.get(current)
            if participant is not None:
                if participant.protected():
                    raise RuntimeError("consumer_web_busy")
                await participant.close()
            self._current = name

    @asynccontextmanager
    async def admit(self, service: str):
        name = str(service or "").strip()
        async with self._admission_lock:
            total_waiting = sum(self._waiting_by_service.values())
            if self._active_service and total_waiting >= 1:
                raise RuntimeError("consumer_web_busy")
            self._waiting_by_service[name] = self._waiting_by_service.get(name, 0) + 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._admission.acquire(), timeout=5.0)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("consumer_web_busy") from exc
            async with self._admission_lock:
                self._waiting_by_service[name] = max(
                    0, self._waiting_by_service.get(name, 0) - 1
                )
                self._active_service = name
            acquired = True
            await self.activate(name)
            yield
        finally:
            if acquired:
                async with self._admission_lock:
                    if self._active_service == name:
                        self._active_service = ""
                self._admission.release()
            else:
                async with self._admission_lock:
                    self._waiting_by_service[name] = max(
                        0, self._waiting_by_service.get(name, 0) - 1
                    )

    def snapshot(self, service: str) -> dict[str, int | bool | str]:
        name = str(service or "").strip()
        return {
            "current_service": self._current,
            "active": self._active_service == name,
            "waiting": max(0, self._waiting_by_service.get(name, 0)),
            "global_waiting": max(0, sum(self._waiting_by_service.values())),
        }


consumer_web_coordinator = ConsumerWebCoordinator()


__all__ = ["ConsumerWebCoordinator", "consumer_web_coordinator"]
