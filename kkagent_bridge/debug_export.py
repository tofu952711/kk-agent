"""Optional local task capture, enabled only by KKGO_DEBUG_EXPORT_DIR."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import uuid


class DebugExport:
    def __init__(self, cfg):
        destination = os.environ.get("KKGO_DEBUG_EXPORT_DIR", "").strip()
        self.enabled = bool(destination)
        self.root = Path(destination).expanduser() if destination else None
        audit = Path(cfg.get("_request_audit_file") or "unassigned")
        self.run_id, _, self.stage = audit.stem.partition("-")
        if self.run_id == "unassigned":
            self.run_id = uuid.uuid4().hex
        self.stage = self.stage or "runtime"
        self.call_id = uuid.uuid4().hex
        self.secret = cfg.get("api_key", "")
        self.directory = self.root / self.run_id if self.enabled else None

    def _clean(self, value, folder):
        if isinstance(value, dict):
            if value.get("type") in ("image", "base64") and isinstance(value.get("data"), str):
                mime = value.get("mimeType", value.get("mime", value.get("media_type", "image/png")))
                return {**{key: self._clean(item, folder) for key, item in value.items() if key != "data"},
                        "data_file": self._image(value["data"], mime, folder)}
            return {key: ("[REDACTED]" if key.lower() in ("api_key", "review_api_key", "authorization", "x-api-key")
                          else self._clean(item, folder)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item, folder) for item in value]
        if isinstance(value, str):
            if value.startswith("data:image/") and ";base64," in value:
                prefix, encoded = value.split(";base64,", 1)
                return {"image_file": self._image(encoded, prefix[5:], folder)}
            return value.replace(self.secret, "[REDACTED]") if self.secret else value
        return value

    def _image(self, encoded, mime, folder):
        data = base64.b64decode(encoded, validate=True)
        digest = hashlib.sha256(data).hexdigest()
        suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(mime, ".bin")
        image_dir = folder / "images"
        image_dir.mkdir(exist_ok=True)
        path = image_dir / (digest + suffix)
        if not path.exists():
            path.write_bytes(data)
        return {"path": "images/" + path.name, "sha256": digest, "bytes": len(data), "mime_type": mime}

    def save(self, name, value):
        if not self.enabled:
            return
        try:
            folder = self.directory / "requests" / self.stage / self.call_id
            folder.mkdir(parents=True, exist_ok=True)
            cleaned = self._clean(value, folder)
            (folder / (name + ".json")).write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
            latest = self.root / ("latest-" + self.call_id + ".tmp")
            latest.write_text(json.dumps({"run_id": self.run_id, "directory": str(self.directory)}, ensure_ascii=False), encoding="utf-8")
            latest.replace(self.root / "latest.json")
        except OSError as exc:
            logging.warning("KK debug export failed: %s", type(exc).__name__)

    def finish(self, cfg):
        if not self.enabled:
            return
        try:
            workspace = Path(cfg["workspace"])
            if workspace.is_dir():
                target = self.directory / "workspaces" / workspace.parent.name
                # Copy task media only; harness configuration and credentials stay out.
                shutil.copytree(workspace, target, dirs_exist_ok=True)
            audit = Path(cfg.get("_request_audit_file") or "")
            for chain in ("a-results", "b-results"):
                source = audit.parent.parent / chain / self.run_id
                if source.is_dir():
                    shutil.copytree(source, self.directory / "results" / chain, dirs_exist_ok=True)
            self.save("export-info", {"run_id": self.run_id, "stage": self.stage,
                      "note": "Request images contain the actual encoded bytes before HTTP submission. Tool-result files preserve raw tool output; request files show what was sent onward. No HTTP headers or credential configuration are exported."})
        except OSError as exc:
            logging.warning("KK debug workspace export failed: %s", type(exc).__name__)
