"""Watch atomic MCP plans and cut locally while the agent keeps responding."""
from __future__ import annotations

import json
import threading
import time

from .video_outputs import build_video_outputs
from .video_segments import validate_segments


class _Superseded(Exception):
    pass


class ParallelVideoOutputs:
    def __init__(self, source, output, info, keep_audio=True, notify=None):
        self.source, self.output, self.info = source, output, info
        self.keep_audio, self.notify = keep_audio, notify
        self._finish = threading.Event()
        self._abort = threading.Event()
        self._thread = threading.Thread(target=self._work, name="kkgo-video-cut")
        self.result = self.error = None
        self.started = time.monotonic()
        self.revision = 0
        self.work_seconds = 0.0

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_):
        self._abort.set()
        self._finish.set()
        # Never leave an encoder reading a job directory after its cleanup.
        self._thread.join()

    def _emit(self, kind, **data):
        if self.notify:
            try:
                self.notify(kind, revision=self.revision, **data)
            except Exception as exc:
                # Preview transport must not turn a valid VIDEO result into a failure.
                print(f"[KKGO] 分段预览不可用：{exc}", flush=True)

    def _plan(self):
        path = self.output / "video_segments.json"
        if not path.is_file():
            return None
        ranges = json.loads(path.read_text(encoding="utf-8"))["segments"]
        return validate_segments(ranges, self.info["duration_seconds"], self.info["fps"])

    def _work(self):
        previous = object()
        while not self._abort.is_set():
            try:
                ranges = self._plan()
                if ranges is None and not self._finish.is_set():
                    self._finish.wait(.05)
                    continue
                if ranges != previous:
                    previous = ranges
                    self.revision += 1
                    self.result = self.error = None
                    self._emit("plan", segments=ranges or [{"start": 0, "end": self.info["duration_seconds"]}])
                    began = time.monotonic()

                    def check_current():
                        if self._abort.is_set() or self._plan() != ranges:
                            raise _Superseded()

                    try:
                        self.result = build_video_outputs(
                            self.source, self.output, self.info, self.keep_audio,
                            ranges=ranges, check_current=check_current,
                            on_clip=lambda index, segment, content, seconds: self._emit(
                                "clip", index=index, segment=segment, content=content, seconds=seconds))
                        finished = time.monotonic()
                        self.result[1].update({
                            "background_started_seconds": round(began - self.started, 3),
                            "background_finished_seconds": round(finished - self.started, 3),
                            "plan_revision": self.revision})
                    except _Superseded:
                        self.result = None
                        continue
                    except Exception as exc:
                        self.error = exc
                    finally:
                        self.work_seconds += time.monotonic() - began
                if self._finish.is_set() and self._plan() == previous:
                    return
                self._finish.wait(.05)
            except Exception as exc:
                self.error = exc
                return

    def finish(self):
        """Called after the final model response; wait only for unfinished cutting."""
        self._finish.set()
        self._thread.join()
        if self.error:
            self._emit("error")
            raise RuntimeError(f"AI已完成回答，但分段视频裁切失败：{self.error}") from self.error
        if self.result is None:
            raise RuntimeError("分段视频任务未生成结果")
        self.result[1]["background_work_seconds"] = round(self.work_seconds, 3)
        self._emit("complete")
        return self.result
