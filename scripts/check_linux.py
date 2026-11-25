"""Read-only deployment check; never install dependencies or start a server."""
from pathlib import Path
import argparse
import importlib
import json
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kkagent_bridge"))
from ffmpeg_segments import executables
from model_paths import model_directory, GROUPS

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path)
    parser.add_argument("--skip-models", action="store_true")
    args = parser.parse_args()
    comfy_root = args.comfy_root or ROOT.parent.parent
    if (comfy_root / "folder_paths.py").is_file():
        sys.path.insert(0, str(comfy_root))
        import folder_paths
        from utils.extra_config import load_extra_path_config
        extra = comfy_root / "extra_model_paths.yaml"
        if extra.is_file():
            load_extra_path_config(str(extra))
    errors = []
    if not sys.platform.startswith("linux"):
        errors.append("This deployment targets Linux")
    if sys.version_info < (3, 10):
        errors.append("Python 3.10+ is required")
    for name in ("numpy", "PIL", "av", "aiohttp", "psutil", "sherpa_onnx", "jsonschema"):
        try:
            importlib.import_module(name)
        except (ImportError, OSError) as exc:
            errors.append(f"Dependency {name}: {type(exc).__name__}")
    try:
        ffmpeg, ffprobe = executables()
    except RuntimeError as exc:
        errors.append(str(exc))
    else:
        try:
            codecs = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=True, timeout=10)
            subprocess.run([ffprobe, "-version"], capture_output=True, check=True, timeout=10)
            if "libx264" not in codecs.stdout:
                errors.append("FFmpeg must include libx264")
        except (OSError, subprocess.SubprocessError):
            errors.append("Bundled FFmpeg/FFprobe could not be executed")
    selected = {}
    if not args.skip_models:
        for group, (_, _, names) in GROUPS.items():
            directory = model_directory(group, comfy_root=comfy_root)
            selected[group] = str(directory)
            for name in names:
                if not (directory / name).is_file():
                    errors.append("Missing model asset: " + str(directory / name))
    print(json.dumps({"python": sys.executable, "platform": sys.platform, "persistent_service": False, "go_required": False,
                      "model_directories": selected, "errors": errors, "ready": not errors}, ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
