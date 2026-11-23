"""Plugin-owned paths; never discover desktop installs or shared credentials."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent
VENDOR_DIR = PACKAGE_DIR / "vendor"


def ensure_dir(path: os.PathLike[str] | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def comfy_base_dir() -> Path:
    """ComfyUI root, resolved through folder_paths when available."""
    try:
        import folder_paths  # type: ignore

        base = folder_paths.base_path
        if base:
            return Path(base)
    except Exception:  # pragma: no cover - depends on ComfyUI runtime
        pass
    # PACKAGE_DIR = <ComfyUI>/custom_nodes/ComfyUI-KKGO-Bridge
    return PACKAGE_DIR.parent.parent


def user_dir() -> Path:
    """ComfyUI per-user directory (`<ComfyUI>/user`)."""
    try:
        import folder_paths  # type: ignore

        getter = getattr(folder_paths, "get_user_directory", None)
        if callable(getter):
            value = getter()
            if value:
                return Path(value)
    except Exception:  # pragma: no cover
        pass
    return comfy_base_dir() / "user"


def bridge_data_dir() -> Path:
    override = os.environ.get("KKGO_DATA_DIR")
    return ensure_dir(Path(override).resolve() if override else user_dir() / "default" / "kkagent_python")


def config_file() -> Path:
    return bridge_data_dir() / "config.json"


def sessions_file() -> Path:
    return bridge_data_dir() / "sessions.json"


def history_file() -> Path:
    return bridge_data_dir() / "history.jsonl"


def media_dir() -> Path:
    """Decoded payloads of the last run, kept so the chat panel can re-attach them."""
    return ensure_dir(bridge_data_dir() / "media")


# Standalone deployments never discover another application's home or credentials.
DEFAULT_KKGO_HOME = str(bridge_data_dir() / "harness")
DEFAULT_WORKSPACE = str(bridge_data_dir() / "workspace")
