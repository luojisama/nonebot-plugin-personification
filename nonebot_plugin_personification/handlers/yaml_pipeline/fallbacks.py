from . import processor as _processor

_EXPORTED = [
    "_group_translation_result",
    "_looks_like_translation_result",
    "_send_translation_forward",
    "_strip_control_markers",
]

globals().update({name: getattr(_processor, name) for name in _EXPORTED})

__all__ = list(_EXPORTED)
