"""Explicit model installation only; no downloads during node import or execution."""
from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
REVISION = "2365baeacb507f821a0c8120fcee3d484dba7a07"
BASE = "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/" + REVISION
ASSETS = [
    {"group": "sensevoice", "name": "model.int8.onnx", "url": BASE + "/model.int8.onnx",
     "mirrors": ["https://github.com/tofu952711/kk-agent/releases/download/models-v1/model.int8.onnx"],
     "sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"},
    {"group": "sensevoice", "name": "tokens.txt", "url": BASE + "/tokens.txt",
     "mirrors": ["https://raw.githubusercontent.com/tofu952711/kk-agent/26bff3a998be54d0700be99fee76dcf27081d441/models/sensevoice/tokens.txt"],
     "sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc"},
    {"group": "speaker-diarization", "name": "segmentation.onnx",
     "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2",
     "archive_sha256": "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
     "member": "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
     "sha256": "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"},
    {"group": "speaker-diarization", "name": "speaker_embedding.onnx",
     "url": "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
     "mirrors": ["https://raw.githubusercontent.com/tofu952711/kk-agent/26bff3a998be54d0700be99fee76dcf27081d441/models/speaker-diarization/speaker_embedding.onnx"],
     "sha256": "f682b514c05d947ee3fa91cd6ec6c5c7543479a128373fa29b1faedccd21fd11"},
]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_paths(package_dir):
    spec = importlib.util.spec_from_file_location("kk_install_model_paths", Path(package_dir) / "kkagent_bridge/model_paths.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _download_one(asset, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Unique temporary files never replace an existing model on download/hash failure.
    with tempfile.TemporaryDirectory(prefix=".kkagent-install-", dir=destination.parent) as work:
        payload = Path(work) / "download"
        request = urllib.request.Request(asset["url"], headers={"User-Agent": "KK-agent-model-installer"})
        with urllib.request.urlopen(request, timeout=120) as response, payload.open("wb") as target:
            shutil.copyfileobj(response, target, 8 * 1024 * 1024)
        if "member" in asset:
            if sha256(payload) != asset["archive_sha256"]:
                raise RuntimeError("Model archive checksum mismatch: " + asset["name"])
            selected = Path(work) / "model"
            with tarfile.open(payload) as archive:
                member = archive.getmember(asset["member"])
                if not member.isfile():
                    raise RuntimeError("Model archive entry is not a regular file")
                with archive.extractfile(member) as source, selected.open("wb") as target:
                    shutil.copyfileobj(source, target, 8 * 1024 * 1024)
            payload = selected
        if sha256(payload) != asset["sha256"]:
            raise RuntimeError("Model checksum mismatch: " + asset["name"])
        try:
            os.link(payload, destination)  # Atomic no-replace publication on the same filesystem.
        except FileExistsError:
            if sha256(destination) != asset["sha256"]:
                raise FileExistsError("Existing model differs; not overwritten: " + str(destination))


def download(asset, destination):
    errors = []
    for url in [asset["url"], *asset.get("mirrors", [])]:
        try:
            _download_one({**asset, "url": url}, destination)
            return
        except (urllib.error.URLError, TimeoutError, RuntimeError, tarfile.TarError) as exc:
            errors.append(type(exc).__name__ + ": " + str(exc))
    raise RuntimeError("Model download failed for " + asset["name"] + "; " + "; ".join(errors))


def install_models(comfy_root, *, package_dir=ROOT, offline=False, assets=None):
    paths = load_paths(package_dir)
    records = []
    for asset in ASSETS if assets is None else assets:
        group = asset["group"]
        directory = paths.model_directory(group, comfy_root=comfy_root, package_dir=package_dir)
        target = directory / asset["name"]
        if not target.exists():
            # Reuse a partial legacy installation file by file as well.
            alternatives = paths.candidates(group, comfy_root=comfy_root, package_dir=package_dir)
            legacy = next((p / asset["name"] for p in alternatives if (p / asset["name"]).is_file()), None)
            if legacy is not None:
                if sha256(legacy) != asset["sha256"]:
                    raise RuntimeError("Legacy model checksum mismatch: " + str(legacy))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(legacy, target)
        if target.exists():
            if sha256(target) != asset["sha256"]:
                raise RuntimeError("Existing model checksum mismatch; not overwritten: " + str(target))
            status = "verified"
        else:
            if offline:
                raise FileNotFoundError("Offline model missing: " + str(target))
            download(asset, target)
            status = "downloaded"
        record = {"path": str(target), "status": status}
        records.append(record)
        print(status + ": " + str(target), flush=True)
    return records
