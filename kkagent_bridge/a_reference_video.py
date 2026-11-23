"""Export A's reference video after full-source evidence preparation."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import subprocess
import time

from .ffmpeg_segments import executables


def prepare_a_reference(video, output: Path, info, timeout=900):
    """Leave source-video.mp4 and all full-length evidence untouched."""
    duration = float(info['duration_seconds'])
    record = {'count': 1, 'split_required': False, 'ai_plan_submitted': False,
              'analysis_duration_seconds': duration, 'source_duration_seconds': duration,
              'reference_limit_seconds': 30.0, 'trimmed': duration > 30,
              'discarded_reference_tail_seconds': max(0.0, duration - 30),
              'plan_origin': 'chain_a_reference_video', 'encoding_seconds': []}
    if duration <= 30:
        record.update(actual_duration_seconds=duration, storage='original VIDEO; full source is within 30 seconds')
        return video, record

    from comfy_api.latest import InputImpl
    ffmpeg, ffprobe = executables()
    source = output.parent / 'source-video.mp4'
    target = output / 'chain-a-reference-30s.mp4'
    began = time.monotonic()

    def run(command):
        remaining = timeout - (time.monotonic() - began)
        if remaining <= 0:
            raise RuntimeError('链路A参考视频裁切超时')
        from .cancellation import cancellable_run
        result = cancellable_run([str(arg) for arg in command], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=remaining,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError('链路A参考视频裁切失败：' + (result.stderr or result.stdout)[-1500:])
        return result.stdout

    limit = 30.0
    for attempt in range(2):
        command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y', '-i', source,
                   '-map', '0:v:0', '-map', '0:a:0?', '-vf', f'trim=start=0:end={limit:.9f},setpts=PTS-STARTPTS',
                   '-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-threads', '2']
        if info.get('audio_tracks'):
            command.extend(['-af', f'atrim=start=0:end={limit:.9f},asetpts=PTS-STARTPTS',
                            '-c:a', 'aac', '-b:a', '192k'])
        command.extend(['-t', f'{limit:.9f}', '-movflags', '+faststart', target])
        run(command)
        probe = json.loads(run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', target]))
        streams = probe['streams']
        video_stream = next(s for s in streams if s['codec_type'] == 'video')
        durations = [float(probe['format']['duration'])] + [float(s['duration']) for s in streams if 'duration' in s]
        actual = max(durations)
        if not math.isfinite(actual) or actual <= 0:
            raise RuntimeError('链路A裁切成品时长无效')
        if actual <= 30.0:
            break
        # Container/audio/frame rounding may extend a fractional-fps endpoint.
        # Remove at most the required boundary frame rather than exceed the cap.
        limit -= max(actual - 30.0, 1 / max(float(info.get('fps') or 30), 1))
    else:
        raise RuntimeError(f'链路A参考视频仍超过30秒：{actual:g}秒，未输出')
    if (video_stream['width'], video_stream['height']) != (info['width'], info['height']):
        raise RuntimeError('链路A参考视频画幅校验失败')
    if any(s['codec_type'] == 'audio' for s in streams) != bool(info.get('audio_tracks')):
        raise RuntimeError('链路A参考视频音轨校验失败')
    # Materialize bytes so downstream consumers survive temporary job cleanup.
    result = InputImpl.VideoFromFile(io.BytesIO(target.read_bytes()))
    record.update(actual_duration_seconds=actual, encoding_seconds=[round(time.monotonic() - began, 3)],
                  storage='in-memory trimmed VIDEO; full source retained for analysis',
                  reference_end_seconds=limit,
                  discarded_reference_tail_seconds=max(0.0, duration - limit))
    return result, record
