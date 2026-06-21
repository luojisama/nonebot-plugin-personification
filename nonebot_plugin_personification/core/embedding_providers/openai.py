from __future__ import annotations

import os
from typing import Any

from .base import EmbeddingProvider


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI-compatible embedding provider."""

    def __init__(self, plugin_config: Any | None = None) -> None:
        self.plugin_config = plugin_config
        self._model_id = (
            str(getattr(plugin_config, "personification_embedding_model", "") or "").strip()
            or "text-embedding-3-small"
        )
        self._api_key = (
            str(getattr(plugin_config, "personification_embedding_api_key", "") or "").strip()
            or os.getenv("OPENAI_API_KEY", "")
        )
        self._base_url = str(getattr(plugin_config, "personification_embedding_api_url", "") or "").strip()

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        model = self._model_id.lower()
        if "large" in model:
            return 3072
        if "small" in model or "ada" in model:
            return 1536
        return 0

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not self._api_key:
            raise RuntimeError("OpenAI embedding api key is empty")
        try:
            from openai import AsyncOpenAI
        except Exception as exc:
            raise RuntimeError(f"openai package unavailable: {exc}") from exc
        kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            kwargs["base_url"] = self._base_url
        client = AsyncOpenAI(**kwargs)
        response = await client.embeddings.create(
            model=self._model_id,
            input=[str(text or "") for text in texts],
        )
        return [list(item.embedding or []) for item in response.data]


__all__ = ["OpenAIEmbeddingProvider"]
