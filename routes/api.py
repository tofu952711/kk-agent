from __future__ import annotations

_REGISTERED = False


def ensure_registered():
    global _REGISTERED
    if _REGISTERED:
        return True
    try:
        from aiohttp import web
        from server import PromptServer
    except ImportError:
        return False
    server = getattr(PromptServer, "instance", None)
    if server is None:
        return False
    from ..kkagent_bridge.cancellation import install_interrupt_hook
    install_interrupt_hook(server)

    @server.routes.get("/kkagent_runninghub/status")
    async def status(request):
        from ..kkagent_bridge.python_backend import runtime_status
        return web.json_response({**runtime_status(), "runtime_ready": True,
                                  "node_ids": ["KKGO_FullVideoAgent", "KKGO_VideoSegments", "KKGO_VideoReverse"]})

    _REGISTERED = True
    return True
