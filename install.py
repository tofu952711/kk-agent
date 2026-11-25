"""ComfyUI Manager install hook; source install and optional legacy offline bundles."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def find_comfy_root(explicit=None):
    configured = explicit or os.environ.get("COMFYUI_PATH")
    candidates = [Path(configured)] if configured else [Path.cwd(), *Path(__file__).absolute().parents]
    for path in candidates:
        if (path / "folder_paths.py").is_file() and (path / "models").is_dir():
            return path.resolve()
    raise RuntimeError("Cannot locate ComfyUI; pass --comfy-root /path/to/ComfyUI")


def asset_installer():
    spec = importlib.util.spec_from_file_location("kkagent_install_assets", ROOT / "scripts/model_assets.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_offline_dependencies(assets):
    manifest = ROOT / "bundle-manifest.json"
    if not manifest.is_file():
        raise RuntimeError("--offline requires a complete offline release with bundle-manifest.json")
    for name, metadata in json.loads(manifest.read_text(encoding="utf-8"))["files"].items():
        path = ROOT / name
        if not path.resolve().is_relative_to(ROOT) or not path.is_file() or assets.sha256(path) != metadata["sha256"]:
            raise RuntimeError("Offline bundle file missing or changed: " + name)
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in ((3, 10), (3, 11), (3, 12), (3, 13)):
        raise RuntimeError("Offline wheels support CPython 3.10-3.13")
    lock = ROOT / "requirements-offline.txt"
    if not lock.exists():  # Older complete bundles used requirements.txt as their lock.
        lock = ROOT / "requirements.txt"
    environment = os.environ.copy()
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index",
                    "--find-links", str(ROOT / "vendor/wheels"), "--only-binary=:all:",
                    "--disable-pip-version-check", "-r", str(lock)], env=environment, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path)
    parser.add_argument("--offline", action="store_true", help="Verify bundle and install local wheels without network")
    args = parser.parse_args()
    if not sys.platform.startswith("linux") or platform.machine().lower() not in ("x86_64", "amd64"):
        raise RuntimeError("This plugin currently supports Linux x86_64")
    comfy_root = find_comfy_root(args.comfy_root)
    # Match ComfyUI's startup path configuration before selecting installation directories.
    sys.path.insert(0, str(comfy_root))
    import folder_paths
    from utils.extra_config import load_extra_path_config
    extra = comfy_root / "extra_model_paths.yaml"
    if extra.is_file():
        load_extra_path_config(str(extra))
    assets = asset_installer()
    if args.offline:
        install_offline_dependencies(assets)
    for name in ("ffmpeg", "ffprobe"):
        path = ROOT / "vendor/ffmpeg/linux-x86_64/bin" / name
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
    # Check prerequisites before a potentially large model download. No runtime pip installs.
    checks = [sys.executable, str(ROOT / "scripts/check_linux.py"), "--comfy-root", str(comfy_root)]
    subprocess.run([*checks, "--skip-models"], check=True)
    assets.install_models(comfy_root, offline=args.offline)
    subprocess.run(checks, check=True)
    print("KK agent installation verified. Restart ComfyUI to load the plugin.")


if __name__ == "__main__":
    main()
