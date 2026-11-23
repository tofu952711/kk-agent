"""Install the full Linux bundle using local files only."""
from pathlib import Path
import hashlib
import json
import os
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    if not sys.platform.startswith("linux") or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("完整包适用于 Linux x86_64")
    if sys.implementation.name != "cpython" or sys.version_info[:2] not in ((3, 10), (3, 11), (3, 12), (3, 13)):
        raise RuntimeError("离线依赖适用于 CPython 3.10—3.13")
    manifest = ROOT / "bundle-manifest.json"
    if not manifest.is_file():
        raise RuntimeError("请使用 GitHub Release 中的完整交付包，源码 ZIP 不含离线资源")
    files = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    for name, metadata in files.items():
        path = ROOT / name
        if not path.is_file() or sha256(path) != metadata["sha256"]:
            raise RuntimeError("完整包文件缺失或校验失败：" + name)
    for name in ("ffmpeg", "ffprobe"):
        path = ROOT / "vendor" / "ffmpeg" / "linux-x86_64" / "bin" / name
        path.chmod(path.stat().st_mode | 0o111)
    environment = os.environ.copy()
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_CONFIG_FILE=os.devnull)
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "install", "--no-index",
                    "--find-links", str(ROOT / "vendor" / "wheels"), "--only-binary=:all:",
                    "--disable-pip-version-check", "-r", str(ROOT / "requirements.txt")],
                   env=environment, check=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "check_linux.py")], check=True)
    print("完整包离线安装完成；重启 ComfyUI 后使用。")


if __name__ == "__main__":
    main()
