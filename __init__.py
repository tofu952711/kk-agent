"""Linux/RunningHub edition using task-local Python; keeps existing KKGO IDs."""
from __future__ import annotations

import sys
import os

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

if sys.platform.startswith("linux"):
    import atexit
    from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
    from .kkagent_bridge import BRIDGE
    from .kkagent_bridge.python_backend import close_all_runtimes
    from .routes.api import ensure_registered

    if os.environ.get("KKGO_ENABLE_FRONTEND", "1").lower() not in ("0", "false", "no"):
        WEB_DIRECTORY = "./web"
    ensure_registered()

    def _shutdown():
        BRIDGE.close()
        close_all_runtimes()

    atexit.register(_shutdown)
    print("[KK Python] Linux nodes loaded; no persistent service or listening port")
else:
    print("[KK Python] Linux edition inactive on this platform")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
