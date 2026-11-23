"""Bridge configuration: JSON file under the ComfyUI user directory."""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from . import paths

def connection_preset(settings: dict[str, Any]) -> dict[str, Any]:
    """Give CodeZ its own provider identity; keep user connection settings."""
    out = dict(settings)
    if out.get("api_protocol") == "codez":
        out["provider"] = "codez"
    return out


DEFAULTS: dict[str, Any] = {
    "backend": "python",
    "enabled": True,
    "transport": "python",
    "kkgo_home": paths.DEFAULT_KKGO_HOME,
    "api_key": "",
    # The harness inherits this process's environment, and a launcher that exports
    # HTTP(S)_PROXY points every model request at that proxy. A dead local proxy
    # then fails inside fetch and the harness reports it as a generic
    # `code: TRANSPORT` — the model looks like it returned an empty reply, and
    # retries never recover. api.deepseek.com is reachable directly on the machines
    # this runs on, so the proxy variables are dropped by default. Set this to
    # False to keep the inherited values, or set `proxy_url` to force one.
    "strip_proxy_env": True,
    "proxy_url": "",
    "profile": "video",
    "workspace": paths.DEFAULT_WORKSPACE,
    "api_protocol": "openai-completions",
    "provider": "apimart",
    "model": "gpt-6.1-sol",
    "base_url": "https://api.apimart.ai/v1",
    "max_tokens": 8192,
    "reasoning_effort": "auto",
    "stream_usage": False,
    "warmup_on_start": False,
    "startup_timeout_s": 120,
    "request_timeout_s": 300,
    "max_image_side": 1568,
    "max_images": 12,
    "max_videos": 4,
    "max_frames": 12,
    "default_sample_frames": 6,
    # Contact-sheet geometry for the video-reading tasks. `sheet_cell_long` is the
    # long edge of one cell; 384-400 is the floor before garment texture and facial
    # structure stop being readable. `sheet_max_side` must stay at or below the
    # per-image budget (1568) or the encoder rescales the sheet a second time.
    "sheet_cell_long": 400,
    "sheet_gap": 4,
    "sheet_max_side": 1568,
    "enable_panel": True,
    "enable_frontend_ui": True,
    "enable_node_ui": True,
    "legacy_nodes": False,
    "session_prefix": "kkgo",

}

_ENV_OVERRIDES = {
    "KKGO_MODEL": "model",
    "KKGO_BASE_URL": "base_url",
}

_CACHE: dict[str, Any] | None = None

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return bool(text)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load(refresh: bool = False) -> dict[str, Any]:
    """Load defaults + config.json + environment overrides."""
    global _CACHE
    if _CACHE is not None and not refresh:
        return _CACHE

    cfg = copy.deepcopy(DEFAULTS)
    path = paths.config_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg = _merge(cfg, data)
                # Existing 0.3.0 installations used the native DeepSeek route.
                if "api_protocol" not in data and data.get("provider") == "deepseek-official":
                    cfg["api_protocol"] = "deepseek-messages"
        except (OSError, json.JSONDecodeError):
            pass
    for env_key, field in _ENV_OVERRIDES.items():
        value = os.environ.get(env_key)
        if value:
            cfg[field] = value
    for flag in ("enabled", "warmup_on_start", "strip_proxy_env", "enable_panel",
                 "enable_frontend_ui", "enable_node_ui", "legacy_nodes"):
        cfg[flag] = _as_bool(cfg[flag])
    cfg["kkgo_home"] = os.environ.get("KKGO_HOME") or cfg.get("kkgo_home") or paths.DEFAULT_KKGO_HOME
    cfg = connection_preset(cfg)
    # An environment key is used when no key is saved; never persist it implicitly.
    env_key = environment_api_key(cfg)
    if env_key and not cfg.get("api_key"):
        cfg["api_key"] = env_key
    _CACHE = cfg
    return cfg


def save(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist a partial update and refresh the cache."""
    global _CACHE
    path = paths.config_file()
    saved = {}
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict):
            raise ValueError("config.json 必须为 JSON 对象")
        if "api_protocol" not in saved and saved.get("provider") == "deepseek-official":
            saved["api_protocol"] = "deepseek-messages"
    # Persist defaults + disk values + explicit edits, never resolved env secrets.
    merged = connection_preset(_merge(_merge(copy.deepcopy(DEFAULTS), saved), updates))
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    _CACHE = None
    return load(refresh=True)


def reset_cache() -> None:
    global _CACHE
    _CACHE = None


def environment_api_key(cfg: dict[str, Any]) -> str:
    if cfg.get("api_protocol") == "codez":
        return os.environ.get("CODEZ_API_KEY") or ""
    names = ("APIMART_API_KEY", "OPENAI_API_KEY") if cfg.get("api_protocol") == "openai-completions" else ("DEEPSEEK_API_KEY",)
    return os.environ.get("KKGO_API_KEY") or next((os.environ[name] for name in names if os.environ.get(name)), "")


def public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Never return API credentials to status polling or exported workflows."""
    return {**{key: value for key, value in cfg.items() if key != "api_key"},
            "has_api_key": bool(cfg.get("api_key"))}


def validate_updates(updates: Any) -> dict[str, Any]:
    from urllib.parse import urlparse
    if not isinstance(updates, dict):
        raise ValueError("设置必须为 JSON 对象")
    allowed = {"api_key", "clear_api_key", "base_url", "model", "provider", "api_protocol", "max_tokens", "reasoning_effort", "stream_usage",
               "request_timeout_s", "startup_timeout_s", "proxy_url", "strip_proxy_env"}
    if set(updates) - allowed:
        raise ValueError("未知设置：" + ", ".join(sorted(set(updates) - allowed)))
    out = dict(updates)
    if "api_protocol" in out and out["api_protocol"] not in ("openai-completions", "deepseek-messages", "codez"):
        raise ValueError("请选择 OpenAI Chat Completions、DeepSeek Messages 或 CodeZ 协议")
    if "reasoning_effort" in out and out["reasoning_effort"] not in ("auto", "low", "medium", "high"):
        raise ValueError("思考等级请选择 auto、low、medium 或 high")
    for field in ("model", "provider", "api_key", "base_url", "proxy_url"):
        if field in out:
            if not isinstance(out[field], str):
                raise ValueError(f"{field} 必须是文字")
            out[field] = out[field].strip()
    for field in ("model", "provider"):
        if field in out and not out[field]:
            raise ValueError(f"{field} 不能为空")
    for field in ("base_url", "proxy_url"):
        if field in out and out[field]:
            parsed = urlparse(out[field])
            if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{field} 需要不含凭据的 http(s) 地址")
            if parsed.query or parsed.fragment:
                raise ValueError(f"{field} 不应带查询参数或片段")
    if out.get("base_url"):
        out["base_url"] = out["base_url"].rstrip("/")
        if out["base_url"].endswith("/chat/completions"):
            out["base_url"] = out["base_url"][:-len("/chat/completions")]
    if "provider" in out:
        import re
        if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", out["provider"]):
            raise ValueError("供应商 ID 需要小写英文字母、数字或连字符")
    for field, lower, upper in (("max_tokens", 256, 65536), ("request_timeout_s", 10, 7200),
                                ("startup_timeout_s", 10, 300)):
        if field in out and (type(out[field]) is not int or not lower <= out[field] <= upper):
            raise ValueError(f"{field} 必须是 {lower}–{upper} 的整数")
    for field in ("strip_proxy_env", "clear_api_key", "stream_usage"):
        if field in out and type(out[field]) is not bool:
            raise ValueError(f"{field} 必须是开关")
    clear = out.pop("clear_api_key", False)
    if clear:
        out["api_key"] = ""
    elif not out.get("api_key"):
        out.pop("api_key", None)
    return connection_preset(out)
