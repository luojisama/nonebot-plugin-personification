from __future__ import annotations

import hashlib
import math
import re
from typing import Any


EMBED_DIM = 64
EMBED_MODEL_VERSION = "hash-bow-v2-stable"
TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{1,8}|[A-Za-z0-9_#@.+-]{2,32}")


def normalize_text(value: Any) -> str:
    text = str(value or "").strip()
    return re.sub(r"\s+", " ", text)


def tokenize(text: str) -> list[str]:
    normalized = normalize_text(text).lower()
    if not normalized:
        return []
    return [token for token in TOKEN_RE.findall(normalized) if token]


def _stable_bucket(token: str, dim: int) -> int:
    digest = hashlib.blake2b(
        token.encode("utf-8"),
        digest_size=8,
        person=b"pers-emb-v2",
    ).digest()
    return int.from_bytes(digest, "big") % max(1, int(dim or EMBED_DIM))


def embed_text(text: str, dim: int = EMBED_DIM) -> list[float]:
    vector = [0.0] * dim
    tokens = tokenize(text)
    if not tokens:
        return vector
    for token in tokens:
        vector[_stable_bucket(token, dim)] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [round(value / norm, 6) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right)))
