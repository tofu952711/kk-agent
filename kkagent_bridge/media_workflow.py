"""Stage ComfyUI VIDEO and connect bounded local media tools to Harness."""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys


def stage_video(video, destination):
    # The stream source can precede crop/trim edits; let VideoInput apply them.
    video.save_to(str(destination))


def configure_media(bridge, video, model_directory=""):
    return configure_media_config(bridge.cfg, video, model_directory)


def configure_media_config(cfg, video, model_directory=""):
    """Stage media independently of AI credentials or a chain's model rules."""
    root = Path(cfg["workspace"])
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source-video.mp4"
    stage_video(video, source)
    output = root / "media-analysis"
    output.mkdir()
    # Keep explicit/legacy paths while allowing ComfyUI's shared model configuration.
    from .model_paths import model_directory as resolve_model_directory
    model_dir = resolve_model_directory("sensevoice", model_directory)
    cfg["media_model_dir"] = str(model_dir)
    # Media tools use ComfyUI's Python (PyAV/Pillow/numpy); Harness still runs in
    # its own SDK runtime, including bundled Python on portable Windows.
    command = [str(Path(__file__).with_name("media_server.py")), "--video", str(source), "--output", str(output)]
    if model_dir.is_dir():
        command.extend(["--model-dir", str(model_dir)])
    from .ffmpeg_segments import executables
    ffmpeg, ffprobe = executables()
    media_env = {"PYTHONIOENCODING": "utf-8",
                 "KKGO_SPEAKER_MODEL_DIR": str(resolve_model_directory("speaker-diarization")),
                 "KKGO_FFMPEG": ffmpeg, "KKGO_FFPROBE": ffprobe}
    # Harness MCP stdio does not inherit arbitrary parent environment variables.
    for name in ("KKGO_SPEAKER_MODEL_DIR",):
        if name in os.environ:
            media_env[name] = os.environ[name]
    cfg["media_mcp"] = {"serverName": "video", "transport": "stdio", "command": sys.executable,
        "args": ["-u", *command], "env": media_env, "cwd": str(root),
        "startupTimeoutMs": 30000, "toolCallTimeoutMs": 180000, "closeTimeoutMs": 5000,
        "failOnStartupError": True, "reconnect": {"enabled": False}}
    return output


MEDIA_BRIEF = """本次素材：@视频1为原视频，@图片1为人物参考，@图片2为商品参考。
原视频已接入本地MCP视频工具：inspect_video（视频信息）、detect_shots（切镜候选）、get_frames（按秒取帧）、transcribe_audio（音轨自动转写）、analyze_voices（声学变化）、diarize_speakers（估计说话人数、每人时间段及声学音色特征，CPU按需加载）。
传入的两张图片依次为人物图和商品图；视频工具返回的图片属于@视频1的抽帧。
"""


def evidence(output):
    result = {}
    for name in ("shot_candidates.json", "transcript.json", "voice_evidence.json", "speaker_evidence.json", "audio_binding_evidence.json", "preparation.json", "video_segments.json", "segment_request.json", "duration_policy.json"):
        path = output / name
        if path.exists():
            result[name] = json.loads(path.read_text(encoding="utf-8"))
    activity = output / "tool_activity.jsonl"
    if activity.exists():
        result["tool_activity"] = [json.loads(line) for line in activity.read_text(encoding="utf-8").splitlines()]
    return result
