from __future__ import annotations

import asyncio

from ._loader import load_personification_module

hash_bow = load_personification_module("plugin.personification.core.embedding_providers.hash_bow")
openai = load_personification_module("plugin.personification.core.embedding_providers.openai")
gemini = load_personification_module("plugin.personification.core.embedding_providers.gemini")


def test_hash_bow_provider_matches_legacy_dim() -> None:
    provider = hash_bow.HashBowEmbeddingProvider()
    vectors = asyncio.run(provider.embed_batch(["hello world"]))

    assert provider.model_id == "hash-bow-v2-stable"
    assert provider.dim == 64
    assert len(vectors) == 1
    assert len(vectors[0]) == 64


def test_real_provider_placeholders_keep_target_model_metadata() -> None:
    openai_provider = openai.OpenAIEmbeddingProvider()
    gemini_provider = gemini.GeminiEmbeddingProvider()

    assert openai_provider.model_id == "text-embedding-3-small"
    assert openai_provider.dim == 1536
    assert gemini_provider.model_id == "gemini-embedding-001"
    assert gemini_provider.dim == 768
