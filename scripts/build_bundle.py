"""Build a source release or a complete backwards-compatible offline bundle."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import stat
import sys
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def included_files(source=False):
    if source:
        names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).decode().split("\0")
        candidates = [ROOT / name for name in set(names) if name]
    else:
        candidates = ROOT.rglob("*")
    for path in sorted(candidates):
        if (path.is_file() and not {"__pycache__", ".git", ".pytest_cache"}.intersection(path.relative_to(ROOT).parts)
                and path.suffix not in (".pyc", ".pyo") and path.name not in ("bundle-manifest.json", "source-manifest.json")
                and (not source or path.suffix not in (".onnx", ".whl", ".zip", ".log")
                     and "vendor" not in path.relative_to(ROOT).parts)):
            yield path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", action="store_true", help="Exclude model weights and bundled executables/wheels")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if destination.exists() or destination.is_relative_to(ROOT):
        raise ValueError("Destination must be a new ZIP outside the plugin folder")
    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)[1]
    files = list(included_files(args.source))
    manifest = {"version": version, "kind":"source" if args.source else "offline",
                "platform":"Linux x86_64 glibc >=2.28", "python":"CPython 3.10+ (offline wheels: 3.10-3.13)", "files":{}}
    for path in files:
        manifest["files"][path.relative_to(ROOT).as_posix()] = {
            "bytes":path.stat().st_size, "sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    manifest_name = "source-manifest.json" if args.source else "bundle-manifest.json"
    if not args.source:
        (ROOT / manifest_name).write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    with zipfile.ZipFile(destination,"x",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in files:
            name = ROOT.name + "/" + path.relative_to(ROOT).as_posix()
            entry = zipfile.ZipInfo.from_file(path,name)
            entry.compress_type = zipfile.ZIP_DEFLATED
            executable = path.name in ("ffmpeg","ffprobe") or path.suffix == ".sh"
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (0o755 if executable else 0o644)) << 16
            archive.writestr(entry,path.read_bytes())
        archive.writestr(ROOT.name + "/" + manifest_name, json.dumps(manifest, ensure_ascii=False, indent=2)+"\n")
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
