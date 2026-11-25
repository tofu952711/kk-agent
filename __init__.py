"""Linux/RunningHub edition using task-local Python; keeps existing KKGO IDs."""
from __future__ import annotations

import sys
import os

from comfy_api.latest import ComfyExtension

if sys.platform.startswith("linux"):
    import atexit
    from .nodes import NODE_LIST
    from .kkagent_bridge import BRIDGE
    from .kkagent_bridge.python_backend import close_all_runtimes
    from .routes.api import ensure_registered

    if os.environ.get("KKGO_ENABLE_FRONTEND", "1").lower() not in ("0", "false", "no"):
        WEB_DIRECTORY = "./web"

    def _shutdown():
        BRIDGE.close()
        close_all_runtimes()

    atexit.register(_shutdown)
    print("[KK Python] Linux nodes loaded; no persistent service or listening port")
else:
    NODE_LIST = []
    print("[KK Python] Linux edition inactive on this platform")

class KKAgentExtension(ComfyExtension):
    async def get_node_list(self):
        return NODE_LIST

    async def on_load(self):
        if sys.platform.startswith("linux"):
            from .kkagent_bridge.model_paths import register_model_paths
            register_model_paths()
            ensure_registered()


async def comfy_entrypoint():
    return KKAgentExtension()


__all__ = ["comfy_entrypoint"]
