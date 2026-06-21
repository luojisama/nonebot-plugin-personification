from __future__ import annotations

import asyncio
import os
from typing import Any

from .base import EmbeddingProvider


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Google Gemini embedding provider."""

    def __init__(self, plugin_config: Any | None = None) -> None:
        self.plugin_config = plugin_config
        self._model_id = (
            str(getattr(plugin_config, "personification_embedding_model", "") or "").strip()
            or "gemini-embedding-001"
        )
        self._api_key = (
            str(getattr(plugin_config, "personification_embedding_api_key", "") or "").strip()
            or os.getenv("GOOGLE_API_KEY", "")
            or os.getenv("GEMINI_API_KEY", "")
        )

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return 768

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise RuntimeError("Gemini embedding api key is empty")
        try:
            import google.generativeai as genai
        except Exception as exc:
            raise RuntimeError(f"google-generativeai package unavailable: {exc}") from exc
        genai.configure(api_key=self._api_key)

        async def _embed_one(text: str) -> list[float]:
            def _call() -> list[float]:
                response = genai.embed_content(
                    model=self._model_id,
                    content=str(text or ""),
                    task_type="retrieval_document",
                )
                values = response.get("embedding") if isinstance(response, dict) else None
                return list(values or [])

            return await asyncio.to_thread(_call)

        return [await _embed_one(text) for text in texts]


__all__ = ["GeminiEmbeddingProvider"]
