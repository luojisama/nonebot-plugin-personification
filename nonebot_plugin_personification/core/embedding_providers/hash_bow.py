from __future__ import annotations

from .base import EmbeddingProvider
from ..embedding_index import EMBED_DIM, EMBED_MODEL_VERSION, embed_text


class HashBowEmbeddingProvider(EmbeddingProvider):
    @property
    def model_id(self) -> str:
        return EMBED_MODEL_VERSION

    @property
    def dim(self) -> int:
        return EMBED_DIM

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [embed_text(text) for text in list(texts or [])]


__all__ = ["HashBowEmbeddingProvider"]
