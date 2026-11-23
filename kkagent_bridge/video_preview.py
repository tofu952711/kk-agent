"""Optional early VIDEO previews in Comfy's bounded temporary storage."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

_LOCK = threading.Lock()
_ACTIVE = set()
_LATEST = {}


def _remove(root, path):
    # Only private direct children created by this module may be removed.
    if path.parent == root and path.name.startswith("kkgo-preview-") and not path.is_symlink():
        try:
            shutil.rmtree(path)
        except OSError:
            pass  # Windows may still be serving a playback request.


class VideoPreview:
    def __init__(self, node_id):
        self.node_id = str(node_id) if node_id is not None else None
        self.run_id = uuid.uuid4().hex
        self.folder = None
        self.server = None
        if self.node_id is not None:
            try:
                import folder_paths
                from server import PromptServer
                server = PromptServer.instance
                if callable(getattr(server, "send_sync", None)):
                    self.server = server
                    self.sid = getattr(server, "client_id", None)
                    self.root = Path(folder_paths.get_temp_directory()).resolve()
            except (ImportError, AttributeError):
                pass

    def _send(self, kind, **data):
        if self.server:
            self.server.send_sync("kkgo_video_segments", {
                "node_id": self.node_id, "run_id": self.run_id, "kind": kind, **data}, self.sid)

    def __enter__(self):
        if self.server:
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                with _LOCK:
                    key = (self.sid, self.node_id)
                    old = _LATEST.get(key)
                    if old and old not in _ACTIVE:
                        _remove(self.root, old)
                    candidates = sorted((p for p in self.root.glob("kkgo-preview-*")
                                         if p.is_dir() and not p.is_symlink() and p not in _ACTIVE),
                                        key=lambda p: p.stat().st_mtime, reverse=True)
                    for index, path in enumerate(candidates):
                        if index >= 15 or time.time() - path.stat().st_mtime > 6 * 3600:
                            _remove(self.root, path)
                    self.folder = Path(tempfile.mkdtemp(prefix="kkgo-preview-", dir=self.root))
                    _ACTIVE.add(self.folder)
                    _LATEST[key] = self.folder
                self._send("start")
            except Exception as exc:
                print(f"[KKGO] 无法建立分段预览：{exc}", flush=True)
                self.server = None
        return self

    def __exit__(self, exc_type, *_):
        if self.folder:
            with _LOCK:
                _ACTIVE.discard(self.folder)
                if exc_type:
                    _remove(self.root, self.folder)
            if exc_type:
                try:
                    self._send("error")
                except Exception:
                    pass

    def notify(self, kind, revision=0, **data):
        try:
            self._notify(kind, revision=revision, **data)
        except Exception as exc:
            print(f"[KKGO] 分段预览不可用：{exc}", flush=True)

    def _notify(self, kind, revision=0, **data):
        if not self.server or not self.folder:
            return
        if kind == "plan":
            for path in self.folder.glob("*.mp4"):
                try:
                    path.unlink()
                except OSError:
                    pass
        if kind == "clip":
            content = data.pop("content")
            filename = f"r{revision}-video{data['index']}.mp4"
            target = self.folder / filename
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(target)
            data["file"] = {"filename": filename, "subfolder": self.folder.name, "type": "temp"}
        self._send(kind, revision=revision, **data)
