"""Bounded FFmpeg cutting used only by the AI-invoked media tool."""
from __future__ import annotations

import bisect
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid


def executables():
    bundled = Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg" / "linux-x86_64" / "bin"
    result = []
    for name in ("ffmpeg", "ffprobe"):
        local = bundled / name
        found = str(local) if local.is_file() else shutil.which(name)
        if not found:
            raise RuntimeError(f"缺少 {name}，请使用包含媒体工具的完整交付包")
        result.append(found)
    return result


def cut_tool_videos(source, output, info, ranges, keep_audio=True, timeout=900):
    """Cut AI-chosen ranges, then verify duration, audio and full frame coverage.

    This mirrors the local Codex video segmenter's FFmpeg/FFprobe method. Only
    physical frame alignment is automatic; all semantic cut points come from AI.
    """
    ffmpeg, ffprobe = executables()
    started = time.monotonic()

    def run(command):
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeError("AI分段工具裁切超时")
        try:
            try:
                from .cancellation import cancellable_run
            except ImportError:  # MCP runs media_tools as a top-level module.
                from cancellation import cancellable_run
            result = cancellable_run([str(arg) for arg in command], capture_output=True,
                encoding="utf-8", errors="replace", timeout=remaining,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("AI分段工具裁切超时") from exc
        if result.returncode:
            raise RuntimeError("FFmpeg分段工具失败：" + (result.stderr or result.stdout)[-3000:])
        return result.stdout

    raw = json.loads(run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", source]))
    stamps = [float(frame["best_effort_timestamp_time"]) for frame in raw["frames"]
              if "best_effort_timestamp_time" in frame]
    if not stamps:
        raise ValueError("FFprobe无法读取原视频帧时间")
    origin = stamps[0]
    timeline = [stamp - origin for stamp in stamps]
    boundaries = [0.0]
    for segment in ranges[:-1]:
        cut = segment["end"]
        index = bisect.bisect_left(timeline, cut)
        choices = timeline[max(0, index - 1):min(len(timeline), index + 1)]
        boundaries.append(min(choices, key=lambda value: abs(value - cut)))
    boundaries.append(float(info["duration_seconds"]))
    aligned = [{"start": start, "end": end} for start, end in zip(boundaries, boundaries[1:])]
    if any(item["end"] <= item["start"] for item in aligned):
        raise ValueError("AI切点对齐原始帧后形成空片段，请重新选择切点")
    folder = output / ("segments-" + uuid.uuid4().hex)
    folder.mkdir()
    files, timings, durations, counts = [], [], [], []
    has_audio = bool(info.get("audio_tracks"))
    for index, segment in enumerate(aligned, 1):
        begin = time.monotonic()
        start, end = segment["start"], segment["end"]
        duration = end - start
        target = folder / f"video{index}.mp4"
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-n", "-i", source,
            "-map", "0:v:0", "-vf",
            f"trim=start={start + origin:.9f}:end={end + origin:.9f},setpts=PTS-STARTPTS",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-threads", "4",
            "-fps_mode", "vfr", "-video_track_timescale", "90000"]
        if keep_audio and has_audio:
            command.extend(["-map", "0:a:0?", "-af",
                f"atrim=start={start + origin:.9f}:end={end + origin:.9f},asetpts=PTS-STARTPTS",
                "-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")
        command.extend(["-t", f"{duration:.9f}", "-movflags", "+faststart", target])
        run(command)
        measured = json.loads(run([ffprobe, "-v", "error", "-count_frames", "-show_streams",
            "-show_format", "-of", "json", target]))
        stream = next(item for item in measured["streams"] if item["codec_type"] == "video")
        actual = float(measured["format"].get("duration", stream.get("duration", 0)))
        if abs(actual - duration) > max(.1, 2 / max(float(info["fps"] or 30), 1)):
            raise ValueError(f"FFmpeg输出第{index}段时长异常：计划{duration:g}秒，实际{actual:g}秒")
        output_audio = any(item["codec_type"] == "audio" for item in measured["streams"])
        if output_audio != bool(keep_audio and has_audio):
            raise ValueError(f"FFmpeg输出第{index}段音轨校验失败")
        files.append(str(target.relative_to(output)))
        timings.append(round(time.monotonic() - begin, 3))
        durations.append(actual)
        counts.append(int(stream["nb_read_frames"]))
    if sum(counts) != len(timeline):
        raise ValueError(f"FFmpeg帧覆盖校验失败：原片{len(timeline)}帧，分段合计{sum(counts)}帧")
    return {"count": len(aligned), "segments": aligned, "requested_segments": ranges,
        "files": files, "status": "ready", "materialized": True,
        "executor": "ai_invoked_media_tool", "tool_backend": "ffmpeg",
        "keep_audio": bool(keep_audio), "actual_durations": durations,
        "source_frames": len(timeline), "exported_frames": counts,
        "frame_coverage_validated": True, "encoding_seconds": timings,
        "tool_seconds": round(time.monotonic() - started, 3),
        "encoding": "FFmpeg libx264 CRF18/AAC; FFprobe duration/audio/frame coverage validation",
        "note": "分段成品已在本次AI工具调用中实际裁切并校验。请结束分段阶段，随后独立执行反推。"}
