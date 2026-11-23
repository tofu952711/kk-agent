"""Read-only deployment check; never install dependencies or start a server."""
from pathlib import Path
import importlib
import json
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
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
    for name in ("ffmpeg", "ffprobe"):
        if not shutil.which(name):
            errors.append(f"Missing system executable: {name}")
    if shutil.which("ffmpeg"):
        try:
            codecs = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=True, timeout=10)
            if "libx264" not in codecs.stdout:
                errors.append("System FFmpeg must include libx264")
        except (OSError, subprocess.SubprocessError):
            errors.append("System FFmpeg could not be executed")
    for name in ("sensevoice/model.int8.onnx", "sensevoice/tokens.txt", "speaker-diarization/segmentation.onnx", "speaker-diarization/speaker_embedding.onnx"):
        if not (ROOT / "models" / name).is_file():
            errors.append("Missing model asset: " + name)
    print(json.dumps({"python": sys.executable, "platform": sys.platform, "persistent_service": False, "go_required": False,
                      "errors": errors, "ready": not errors}, ensure_ascii=False, indent=2))
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
