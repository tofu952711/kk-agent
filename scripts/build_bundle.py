"""Rebuild the full offline bundle and its install-time SHA256 manifest."""
from pathlib import Path
import hashlib
import json
import re
import stat
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def included_files():
    for path in sorted(ROOT.rglob("*")):
        if (path.is_file() and not {"__pycache__", ".git", ".pytest_cache"}.intersection(path.relative_to(ROOT).parts)
                and path.suffix not in (".pyc", ".pyo") and path.name != "bundle-manifest.json"):
            yield path


def main():
    destination = Path(sys.argv[1]).resolve()
    if destination.exists() or destination.is_relative_to(ROOT):
        raise ValueError("Destination must be a new ZIP outside the plugin folder")
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)[1]
    files = list(included_files())
    manifest = {"version": version, "platform":"Linux x86_64 glibc >=2.28", "python":"CPython 3.10-3.13", "files":{}}
    for path in files:
        manifest["files"][path.relative_to(ROOT).as_posix()] = {
            "bytes":path.stat().st_size, "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest_path = ROOT / "bundle-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    with zipfile.ZipFile(destination,"x",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in sorted([*files,manifest_path]):
            name = ROOT.name + "/" + path.relative_to(ROOT).as_posix()
            entry = zipfile.ZipInfo.from_file(path,name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            executable = path.name in ("ffmpeg","ffprobe") or path.suffix == ".sh"
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(entry,path.read_bytes())
    # Verify the archive's exact payload against the newly written manifest.
    with zipfile.ZipFile(destination) as archive:
        for name, meta in manifest["files"].items():
            data = archive.read(ROOT.name + "/" + name)
            if len(data) != meta["bytes"] or hashlib.sha256(data).hexdigest() != meta["sha256"]:
                raise RuntimeError("Bundle payload failed verification: " + name)
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC check failed")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    checksum = destination.with_suffix(".zip.sha256")
    checksum.write_text(digest + "  " + destination.name + "\n",encoding="utf-8")
    print(json.dumps({"zip":str(destination),"version":version,"bytes":destination.stat().st_size,
        "verified_files":len(manifest["files"]),"sha256":digest},ensure_ascii=False))


if __name__ == "__main__":
    main()
