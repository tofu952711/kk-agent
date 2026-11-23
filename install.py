"""Install the pinned SenseVoice asset omitted from the Git repository."""
from pathlib import Path
import hashlib
import shutil
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "models" / "sensevoice" / "model.int8.onnx"
MODEL_URL = "https://github.com/tofu952711/kk-agent-runninghub/releases/download/models-v1/model.int8.onnx"
MODEL_SHA256 = "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    if MODEL.is_file() and sha256(MODEL) == MODEL_SHA256:
        print("SenseVoice model is ready.")
        return
    MODEL.parent.mkdir(parents=True, exist_ok=True)
    print("Downloading SenseVoice model (239 MB) from GitHub Release...", flush=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=MODEL.parent, suffix=".download", delete=False) as output:
            temporary = Path(output.name)
            request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "KK-agent-runninghub-installer"})
            with urllib.request.urlopen(request, timeout=120) as response:
                shutil.copyfileobj(response, output)
        if sha256(temporary) != MODEL_SHA256:
            raise RuntimeError("SenseVoice model SHA-256 mismatch; installation stopped.")
        temporary.replace(MODEL)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("SenseVoice model is ready.")


if __name__ == "__main__":
    main()
