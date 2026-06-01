from __future__ import annotations

from .base import EmbeddingProvider
from .hash_bow import HashBowEmbeddingProvider


class GeminiEmbeddingProvider(EmbeddingProvider):
    """Provider placeholder for gemini-embedding-001.

    The class exposes the target model metadata now; actual HTTP wiring can be
    added behind personification_real_embedding_enabled without changing callers.
    """

    @property
    def model_id(self) -> str:
        return "gemini-embedding-001"

    @property
    def dim(self) -> int:
        return 768

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        fallback = HashBowEmbeddingProvider()
        return await fallback.embed_batch(texts)


__all__ = ["GeminiEmbeddingProvider"]
