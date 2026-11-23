"""B-only preparation: disclose and skip undecodable requests beyond the last PTS."""
from __future__ import annotations

from pathlib import Path
import time

from .images import tensor_to_base64
from .media_tools import MediaTools
from .media_workflow import configure_media_config
from .video_preparation import sampling_plan, voice_context, prepared_evidence_text, prepared_images


class BTailMediaTools(MediaTools):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.skipped_tail_times = set()
        self.last_decodable_time = None

    def cache_frames(self, times, *, sequential=False):
        times = self._validate_times(times)
        try:
            super().cache_frames(times, sequential=sequential)
        except ValueError as exc:
            missing = [t for t in times if self._time_key(t) not in self._frame_index]
            # Do not swallow invalid inputs, a broken decoder, or interior misses.
            if str(exc) != "请求时间超出可解码视频范围，请稍向前取帧" or not missing:
                raise
            if self.last_decodable_time is None:
                import av
                with av.open(str(self.video)) as container:
                    stream = container.streams.video[0]
                    origin = float(stream.start_time or 0) * float(stream.time_base)
                    last = None
                    for frame in container.decode(stream):
                        if frame.pts is None:
                            raise ValueError("B尾帧检查需要真实视频PTS，不能推测缺失帧")
                        stamp = float(frame.pts * stream.time_base) - origin
                        last = stamp if last is None else max(last, stamp)
                if last is None:
                    raise ValueError("原视频没有可解码画面，不能跳过整条视频")
                self.last_decodable_time = last
            if any(t <= self.last_decodable_time + 1e-6 for t in missing):
                raise
            self.skipped_tail_times.update(missing)
            print(f"[KKGO] B跳过不可解码片尾采样：{missing}；最后真实帧{self.last_decodable_time:.6f}秒", flush=True)
            self._write_json("b_tail_sampling.json", {
                "last_decodable_time": self.last_decodable_time,
                "skipped_times": sorted(self.skipped_tail_times),
                "policy": "skip requests beyond last decoded PTS; no replacement or fabricated frames"})
        return [t for t in times if self._time_key(t) in self._frame_index]

    def frames(self, times):
        times = self._validate_times(times, limit=12)
        kept = self.cache_frames(times)
        if not kept:
            raise ValueError("本次请求全部位于最后真实帧之后，已跳过；请向前取帧")
        meta, image = self.sheet(kept, cols=1 if len(kept) == 1 else 4, detail=len(kept) == 1)
        meta["skipped_tail_times"] = [t for t in times if t not in kept]
        return meta, image


def prepare_b_video(output, model_dir=None):
    """Keep existing A preparation untouched; B uses the same evidence structure."""
    tools = BTailMediaTools(output.parent / "source-video.mp4", output, model_dir)
    timings = {}
    started = time.monotonic()

    def measured(name, action):
        begin = time.monotonic()
        value = action()
        timings[name] = round(time.monotonic() - begin, 3)
        return value

    info = measured("metadata_seconds", tools.info)
    shots = measured("shot_detection_seconds", tools.shots)
    plan = sampling_plan(info, shots)
    keys = ("overview", "dense", "cut_context", "short")
    requested = sorted(set(t for key in keys for t in plan[key]))
    measured("frame_decode_seconds", lambda: tools.cache_frames(requested, sequential=True))
    valid = lambda t: tools._time_key(t) in tools._frame_index
    overview, dense, short = ([t for t in plan[k] if valid(t)] for k in ("overview", "dense", "short"))
    # Preserve four-frame row alignment; never mislabel following cut candidates.
    groups = [(cut, plan["cut_context"][i*4:i*4+4]) for i, cut in enumerate(plan["selected"])]
    groups = [(cut, times) for cut, times in groups if all(valid(t) for t in times)]
    selected = [cut for cut, _ in groups]
    contexts = [t for _, times in groups for t in times]
    kept = sorted(set(overview + dense + contexts + short))
    if not kept:
        raise ValueError("B预处理未获得可用画面，不能继续反推")
    sheets = []

    def compose():
        for name, kind, times, batch, cols in (
                ("overview", "全片概览", overview, 20, 5),
                ("motion", "全片密集动作与运镜对照", dense, 20, 5),
                ("cut-context", "候选切点四帧对照", contexts, 16, 4),
                ("short-intervals", "密集采样间隙内的短镜头候选中点", short, 20, 5)):
            for begin in range(0, len(times), batch):
                meta, _ = tools.sheet(times[begin:begin+batch], prefix=name, cols=cols)
                item = {"kind": kind, **meta}
                if name == "cut-context":
                    item["candidate_cut_times"] = [c["time"] for c in selected[begin//4:(begin+batch)//4]]
                    item["row_layout"] = "每行一个候选；依次切点前约2帧、前1帧、切点帧、后约3帧；以实际PTS标签为准。"
                sheets.append(item)
    measured("sheet_composition_seconds", compose)

    def transcribe():
        try:
            return tools.transcribe()
        except (ValueError, ImportError) as exc:
            result = {"has_audio": bool(info["audio_tracks"]), "status": "unavailable",
                      "full_text": "", "segments": [], "reason": str(exc)}
            tools._write_json("transcript.json", result)
            return result
    transcript = measured("transcription_seconds", transcribe)
    voices = measured("voice_analysis_seconds", tools.analyze_voices)
    focus = voice_context(voices, transcript, info["duration_seconds"], info["fps"])
    reviewed = []

    def voice_images():
        times = [t for _, samples in focus for t in samples]
        if not times:
            return
        tools.cache_frames(times, sequential=True)
        for boundary, samples in focus:
            samples = [t for t in samples if valid(t)]
            if not samples:
                continue
            meta, _ = tools.sheet(samples, cols=4, prefix="voice-context")
            reviewed.append(samples)
            sheets.append({"kind": "声音变化附近口型与人物回应对照", "candidate_voice_time": boundary,
                           "observation_note": "只提供真实帧；未自动识别唇动或声画同步，抽帧不等于连续听看。", **meta})
    measured("voice_visual_seconds", voice_images)
    tools._write_json("voice_evidence.json", voices)
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    skipped = sorted(tools.skipped_tail_times)
    report = {"version": 3, "video": info, "timings": timings, "sheets": sheets,
              "voice_evidence": voices, "coverage": {
                  "overview_step_seconds": plan["overview_step"], "overview_frames": len(overview),
                  "dense_step_seconds": plan["dense_step"], "dense_frames": len(dense),
                  "candidate_cuts": len(shots["candidate_cuts"]), "previewed_candidate_cuts": len(selected),
                  "previewed_cut_times": [c["time"] for c in selected], "cut_context_frames": len(contexts),
                  "short_interval_frames": len(short), "unique_requested_frames": len(requested),
                  "unique_retained_requests": len(kept),
                  "unique_actual_frames": len({tools._frame_index[tools._time_key(t)]["actual"] for t in kept}),
                  "voice_review_candidates": len(reviewed),
                  "voice_review_requested_frames": sum(map(len, reviewed)),
                  "skipped_tail_times": skipped, "last_decodable_time": tools.last_decodable_time},
              "note": "只取原视频真实帧，没有插帧或补造帧。候选切点须看前后帧核实；抽帧不等于连续观看。"
                      "B链路对最后真实帧之后无法解码的采样点直接跳过，不影响原视频时长和实际裁切范围。"}
    tools._write_json("preparation.json", report)
    return report, shots, transcript


def prepare_b_chain_media(cfg, video, routing, asr_model_dir, image_values, audio_values=(None, None, None)):
    output = configure_media_config(cfg, video, asr_model_dir)
    tools = MediaTools(output.parent / "source-video.mp4", output, Path(cfg["media_model_dir"]))
    tools._write_json("duration_policy.json", {**routing, "phase": "prepared"})
    slots = [i for i, image in enumerate(image_values, 1) if image is not None]
    refs = []
    for index in slots:
        image = image_values[index-1]
        if len(image.shape) == 4 and image.shape[0] != 1:
            raise ValueError(f"image{index}只接收单张图片；请拆开图片批次后连接独立插口")
        refs.append(tensor_to_base64(image, max_side=1568))
    print("[KKGO] 链路B：正在本地预处理视频和音轨转写（不可解码片尾采样自动跳过）", flush=True)
    from .isolated_preparation import prepare_isolated
    report, shots, transcript = prepare_isolated(output, Path(cfg["media_model_dir"]),
        chain="B", timeout=cfg.get("request_timeout_s", 900))
    from .reference_audio import attach_audio_bindings
    attach_audio_bindings(audio_values, output, report)
    text = prepared_evidence_text(report, shots, transcript, slots)
    cfg["video_media_evidence"] = text
    return output, report, slots, refs, prepared_images(output, report), text
