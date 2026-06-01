from __future__ import annotations

from .base import EmbeddingProvider
from .hash_bow import HashBowEmbeddingProvider


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """Provider placeholder for text-embedding-3-small.

    The runtime can instantiate this once an embedding HTTP client is wired.
    Until then callers should fall back to HashBowEmbeddingProvider.
    """

    @property
    def model_id(self) -> str:
        return "text-embedding-3-small"

    @property
    def dim(self) -> int:
        return 1536

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        fallback = HashBowEmbeddingProvider()
        return await fallback.embed_batch(texts)


__all__ = ["OpenAIEmbeddingProvider"]
