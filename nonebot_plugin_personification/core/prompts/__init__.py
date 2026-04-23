from __future__ import annotations

import hashlib
import logging
from pathlib import Path

_DIR = Path(__file__).parent
_LOGGER = logging.getLogger("personification.prompts")


def load_prompt(name: str) -> str:
    path = _DIR / f"{name}.txt"
    content = path.read_text(encoding="utf-8")
    digest = hashlib.md5(content.encode("utf-8")).hexdigest()
    _LOGGER.debug("personification: loaded prompt name=%s md5=%s path=%s", name, digest, path)
    return content
