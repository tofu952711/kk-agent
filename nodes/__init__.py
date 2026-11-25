"""Node package exports."""

from __future__ import annotations

from .kkgo_video_agent import KKGO_FullVideoAgent
from .kkgo_video_stages import KKGO_VideoSegments, KKGO_VideoReverse

NODE_CLASS_MAPPINGS = {"KKGO_FullVideoAgent": KKGO_FullVideoAgent,
    "KKGO_VideoSegments": KKGO_VideoSegments, "KKGO_VideoReverse": KKGO_VideoReverse}
NODE_DISPLAY_NAME_MAPPINGS = {name: cls.define_schema().display_name for name, cls in NODE_CLASS_MAPPINGS.items()}
NODE_LIST = list(NODE_CLASS_MAPPINGS.values())

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "NODE_LIST",
]
