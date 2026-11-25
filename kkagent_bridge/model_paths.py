"""Shared ComfyUI model folders with backwards-compatible plugin-local fallback."""
from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[1]
GROUPS = {
    "sensevoice": ("kkagent_sensevoice", "KKGO_ASR_MODEL_DIR", ("model.int8.onnx", "tokens.txt")),
    "speaker-diarization": ("kkagent_speaker", "KKGO_SPEAKER_MODEL_DIR", ("segmentation.onnx", "speaker_embedding.onnx")),
}


def register_model_paths():
    try:
        import folder_paths
    except ImportError:
        return None
    for group, (category, _, _) in GROUPS.items():
        folder_paths.add_model_folder_path(category, str(Path(folder_paths.models_dir) / "kkagent" / group))
    return folder_paths


def candidates(group, *, comfy_root=None, package_dir=None):
    package = Path(package_dir) if package_dir else PACKAGE_DIR
    paths = []
    folders = register_model_paths() if comfy_root is None else sys.modules.get("folder_paths")
    if folders is not None and (comfy_root is None or
            Path(folders.models_dir).resolve() == (Path(comfy_root) / "models").resolve()):
        register_model_paths()
        category = GROUPS[group][0]
        paths.extend(Path(p) for p in folders.get_folder_paths(category))
    if comfy_root is not None:
        paths.append(Path(comfy_root) / "models" / "kkagent" / group)
    paths.append(package / "models" / group)
    return list(dict.fromkeys(paths))


def model_directory(group, explicit="", *, comfy_root=None, package_dir=None):
    package = Path(package_dir) if package_dir else PACKAGE_DIR
    override = explicit or os.environ.get(GROUPS[group][1], "").strip()
    if override:
        path = Path(override).expanduser()
        return (path if path.is_absolute() else package / path).resolve()
    options = candidates(group, comfy_root=comfy_root, package_dir=package)
    for path in options:
        if all((path / name).is_file() for name in GROUPS[group][2]):
            return path.resolve()
    return options[0].resolve()
