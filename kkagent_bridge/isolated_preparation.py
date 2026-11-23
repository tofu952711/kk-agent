"""Keep PyAV/ASR native stalls out of ComfyUI's process and cancellation path."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from .cancellation import cancellable_run


def prepare_isolated(output, model_dir, *, chain="A", timeout=900):
    output = Path(output)
    log = output / "preparation-worker.log"
    command = [sys.executable, "-u", str(Path(__file__).with_name("preparation_worker.py")),
               str(output), str(model_dir), chain]
    with log.open("wb") as handle:
        result = cancellable_run(command, timeout=timeout, stdout=handle, stderr=subprocess.STDOUT)
    if result.returncode:
        detail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError("视频预处理失败：" + detail)
    return tuple(json.loads((output / "preparation-result.json").read_text(encoding="utf-8")))
