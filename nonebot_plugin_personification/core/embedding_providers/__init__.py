from __future__ import annotations

from typing import Any

from .base import EmbeddingProvider
from .gemini import GeminiEmbeddingProvider
from .hash_bow import HashBowEmbeddingProvider
from .openai import OpenAIEmbeddingProvider


def build_embedding_provider(plugin_config: Any | None = None) -> EmbeddingProvider:
    real_enabled = bool(getattr(plugin_config, "personification_real_embedding_enabled", False))
    provider_name = str(getattr(plugin_config, "personification_embedding_provider", "hash_bow") or "hash_bow")
    provider_name = provider_name.strip().lower().replace("-", "_")
    if not real_enabled:
        return HashBowEmbeddingProvider()
    if provider_name == "gemini":
        return GeminiEmbeddingProvider(plugin_config)
    if provider_name == "openai":
        return OpenAIEmbeddingProvider(plugin_config)
    return HashBowEmbeddingProvider()


__all__ = [
    "EmbeddingProvider",
    "GeminiEmbeddingProvider",
    "HashBowEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "build_embedding_provider",
]
