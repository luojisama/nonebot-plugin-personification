from __future__ import annotations

from typing import Any, Iterable

from .embedding_index import normalize_text, tokenize


def extract_entities(summary: str, topic_tags: Iterable[Any], entity_tags: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    entities: list[str] = []
    for value in list(entity_tags or []) + list(topic_tags or []):
        token = normalize_text(value)
        if token and token not in seen:
            seen.add(token)
            entities.append(token)
    for token in tokenize(summary):
        if len(token) < 2:
            continue
        if token not in seen:
            seen.add(token)
            entities.append(token)
    return entities[:24]
