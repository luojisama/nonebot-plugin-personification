from . import processor as _processor

_EXPORTED = [
    "_build_final_visible_reply_text",
    "_truncate_at_punctuation",
]

globals().update({name: getattr(_processor, name) for name in _EXPORTED})

__all__ = list(_EXPORTED)
