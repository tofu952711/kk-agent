"""Per-run Harness configuration for public workflows; no shared credentials."""
from __future__ import annotations

import copy
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .bridge import Bridge
from .cancellation import watch_bridge
from .config import DEFAULTS, validate_updates


@contextmanager
def workflow_bridge(api_key: str, base_url: str, model: str, timeout_s: int,
                    api_protocol: str = "openai-completions", *, max_tokens: int = 32768,
                    reasoning_effort: str = "auto", stream_usage: bool = False):
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("请在本节点的 API Key 中填写对应服务的密钥")
    settings = validate_updates({"api_key": api_key, "base_url": base_url,
                                 "model": model, "request_timeout_s": timeout_s,
                                 "api_protocol": api_protocol, "max_tokens": max_tokens,
                                 "reasoning_effort": reasoning_effort, "stream_usage": stream_usage})
    if not settings.get("base_url"):
        raise ValueError("Base URL 不能为空")
    with TemporaryDirectory(prefix="kkgo-workflow-") as temporary:
        root = Path(temporary)
        cfg = {**copy.deepcopy(DEFAULTS), **settings,
               "provider": settings.get("provider") or ("deepseek-official" if api_protocol == "deepseek-messages" else "apimart"),
               "kkgo_home": str(root / "harness"), "workspace": str(root / "workspace"),
               "warmup_on_start": False}
        bridge = Bridge(cfg, persist=False)
        try:
            with watch_bridge(bridge):
                yield bridge
        finally:
            bridge.close()
