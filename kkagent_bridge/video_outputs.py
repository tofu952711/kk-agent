"""Materialize independent ComfyUI VIDEO values before job cleanup."""
from __future__ import annotations

import io
import json
from pathlib import Path
import time

from .video_segments import validate_segments


def video_only_source(source: Path):
    """Strip audio without decoding frames, once per run."""
    import av
    buffer = io.BytesIO()
    with av.open(str(source)) as original, av.open(buffer, mode="w", format="mp4") as muted:
        stream = original.streams.video[0]
        target = muted.add_stream_from_template(stream)
        for packet in original.demux(stream):
            if packet.dts is None:
                continue
            packet.stream = target
            muted.mux(packet)
    buffer.seek(0)
    return buffer


def build_video_outputs(source: Path, output: Path, info, keep_audio=True, *,
                        ranges=None, on_clip=None, check_current=None):
    from comfy_api.latest import InputImpl, Types
    from comfy_execution.graph_utils import ExecutionBlocker

    plan_file = output / "video_segments.json"
    planned = ranges is not None or plan_file.is_file()
    if ranges is None:
        ranges = (json.loads(plan_file.read_text(encoding="utf-8"))["segments"] if planned else
                  [{"start": 0, "end": info["duration_seconds"]}])
    ranges = validate_segments(ranges, info["duration_seconds"], info["fps"])
    remove_started = time.monotonic()
    media_source = str(source) if keep_audio else video_only_source(source)
    remove_seconds = round(time.monotonic() - remove_started, 3)
    videos, timings = [], []
    for index, segment in enumerate(ranges, 1):
        if check_current:
            check_current()
        started = time.monotonic()
        buffer = io.BytesIO()
        if len(ranges) == 1:
            # No transcode is needed for an unchanged full source.
            buffer.write(source.read_bytes() if keep_audio else media_source.getvalue())
        else:
            clip = InputImpl.VideoFromFile(media_source).as_trimmed(
                segment["start"], segment["end"] - segment["start"], strict_duration=False)
            clip.save_to(buffer, format=Types.VideoContainer.MP4, codec=Types.VideoCodec.H264,
                         crf=18, preset="fast")
        buffer.seek(0)
        videos.append(InputImpl.VideoFromFile(buffer))
        seconds = round(time.monotonic() - started, 3)
        timings.append(seconds)
        if check_current:
            check_current()
        if on_clip:
            on_clip(index, segment, buffer.getvalue(), seconds)
        print(f"[KKGO] video{index}已就绪：{segment['start']:g}—{segment['end']:g}秒，裁切耗时{seconds}秒", flush=True)
    # An unused branch must not pass an invalid None VIDEO into SaveVideo.
    videos.extend(ExecutionBlocker(None) for _ in range(5 - len(videos)))
    return videos, {"count": len(ranges), "segments": ranges, "ai_plan_submitted": planned,
                    "keep_audio": bool(keep_audio), "remove_audio_seconds": remove_seconds,
                    "encoding_seconds": timings,
                    "storage": "in-memory VIDEO; survives task temporary-directory cleanup",
                    "encoding": "source bytes for whole video; CPU H.264 CRF18/AAC for split segments"}
