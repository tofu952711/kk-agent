"""B-only, segment-scoped evidence and private media-tool workspaces."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from .media_tools import MediaTools
from .b_video_preparation import BTailMediaTools
from .video_preparation import prepared_evidence_text, prepared_images


def scoped_transcript(transcript, start, end):
    result = copy.deepcopy(transcript)
    tokens, stamps = transcript.get("tokens", []), transcript.get("timestamps", [])
    if tokens and len(tokens) == len(stamps):
        pairs = [(token, stamp) for token, stamp in zip(tokens, stamps) if start <= stamp < end]
        result.update(tokens=[p[0] for p in pairs], timestamps=[p[1] for p in pairs],
                      full_text="".join(str(p[0]) for p in pairs))
    else:
        # Coarse ASR windows can straddle a cut; retain and disclose that context.
        windows = [v for v in transcript.get("segments", [])
                   if v.get("end", 0) > start and v.get("start", 0) < end]
        result["full_text"] = "".join(v.get("text", "") for v in windows)
    result["segments"] = [v for v in transcript.get("segments", [])
                          if v.get("end", 0) > start and v.get("start", 0) < end]
    result["assigned_range"] = {"start": start, "end": end}
    result["scope_note"] = "时间为原片秒数；ASR时间为估计值，跨切点的粗窗口仅作上下文，不得整句重复分配到两段。"
    return result


def scoped_voice(voice, start, end):
    result = copy.deepcopy(voice)
    result["windows"] = [v for v in voice.get("windows", [])
                         if v.get("end", 0) > start and v.get("start", 0) < end]
    result["change_candidates"] = [v for v in voice.get("change_candidates", [])
                                   if start <= v["time"] < end]
    result["range"] = {"start": start, "end": end}
    return result


class SegmentMediaTools(BTailMediaTools):
    """Read original audio/video, but write only this B child's private cache."""
    def __init__(self, video, output, model_dir=None):
        super().__init__(video, output, model_dir)
        self.scope = self._read_json("b_segment_scope.json")
        if not self.scope:
            raise ValueError("B子任务缺少已确认的分段范围")

    def _validate_times(self, times, limit=None):
        times = super()._validate_times(times, limit)
        low, high = self.scope["context_start"], self.scope["context_end"]
        if any(not low <= t < high for t in times):
            raise ValueError(f"本子任务仅可查看原片{low:g}—{high:g}秒（含边界上下文），请使用原片秒数")
        return times

    def info(self):
        return {**super().info(), "assigned_segment": self.scope,
                "time_note": "工具时间全部使用原片秒数；交付正文时间=原片时间减去assigned_segment.start。"}

    def frames(self, times):
        meta, image = super().frames(times)
        meta["local_times"] = [round(t - self.scope["start"], 6) for t in meta["actual_times"]]
        meta["context_only"] = [not self.scope["start"] <= t < self.scope["end"]
                                for t in meta["actual_times"]]
        return meta, image

    def shots(self, threshold=.16):
        report = copy.deepcopy(super().shots(threshold))
        start, end = self.scope["start"], self.scope["end"]
        report["candidate_cuts"] = [v for v in report["candidate_cuts"] if start <= v["time"] < end]
        report["candidate_intervals"] = [{**v, "start": max(start, v["start"]), "end": min(end, v["end"])}
                                         for v in report.get("candidate_intervals", [])
                                         if v["end"] > start and v["start"] < end]
        return report

    def analyze_voices(self, start=None, end=None):
        if start is None and end is None:
            return self._read_json("voice_evidence.json") or {}
        start = self.scope["start"] if start is None else float(start)
        end = self.scope["end"] if end is None else float(end)
        if not self.scope["context_start"] <= start < end <= self.scope["context_end"]:
            raise ValueError("声音复核须使用本段范围内的原片秒数")
        return super().analyze_voices(start, end)

    def diarize_speakers(self, num_speakers=0, threshold=.5):
        # Use the entire original recording to keep speaker IDs stable across cuts.
        report = super().diarize_speakers(num_speakers, threshold)
        return {**report, "assigned_segment": self.scope,
                "scope_note": "说话人编号按全片首次出现排序；只将本段交叠的发言用于本段正文。"}

    def set_video_segments(self, segments):
        raise ValueError("B子智能体只反推既定段落，不能修改已完成的分段")


def prepare_segment_bundle(source_output, report, slots, index, segment, count, shared_context):
    """Reuse decoded source frames; recompose mixed sheets at segment boundaries."""
    source_output = Path(source_output)
    output = source_output / "b-reverse" / f"segment-{index}"
    output.mkdir(parents=True, exist_ok=False)
    info = report["video"]
    start, end = segment["start"], segment["end"]
    scope = {"index": index, "count": count, "start": start, "end": end,
             "duration": end - start, "local_start": 0, "local_end": end - start,
             "context_start": max(0., start - .5),
             "context_end": min(info["duration_seconds"], end + .5)}
    (output / "b_segment_scope.json").write_text(json.dumps(scope), encoding="utf-8")
    # Link immutable source frames by relative path; new frames/indexes stay local.
    original_index = json.loads((source_output / "frame_index.json").read_text(encoding="utf-8"))
    private_index = {key: {**value, "file": os.path.relpath(source_output / value["file"], output)}
                     for key, value in original_index.items()
                     if scope["context_start"] <= value["actual"] < scope["context_end"]}
    (output / "frame_index.json").write_text(json.dumps(private_index), encoding="utf-8")
    tools = SegmentMediaTools(source_output.parent / "source-video.mp4", output)
    sheets = []
    seen = set()
    for sheet in report["sheets"]:
        times = [t for t in sheet["requested_times"]
                 if scope["context_start"] <= t < scope["context_end"]]
        # Selection is based on actual decoded PTS, not merely requested seconds.
        times = [t for t in times if tools._time_key(t) in private_index]
        if not times or tuple(times) in seen:
            continue
        seen.add(tuple(times))
        meta, _ = tools.sheet(times, cols=4, prefix="segment-evidence")
        sheets.append({"kind": sheet["kind"], **meta})
    if not sheets:
        meta, _ = tools.frames([start, (start + end) / 2])
        sheets.append({"kind": "本段起始及中点真实帧", **meta})
    transcript = scoped_transcript(json.loads((source_output / "transcript.json").read_text(encoding="utf-8")), start, end)
    shots = json.loads((source_output / "shot_candidates.json").read_text(encoding="utf-8"))
    shots["candidate_cuts"] = [v for v in shots["candidate_cuts"] if start <= v["time"] < end]
    shots["candidate_intervals"] = [{**v, "start": max(start, v["start"]), "end": min(end, v["end"])}
                                    for v in shots.get("candidate_intervals", [])
                                    if v["end"] > start and v["start"] < end]
    voices = scoped_voice(report.get("voice_evidence", {}), start, end)
    for name, value in (("transcript.json", transcript), ("shot_candidates.json", shots),
                        ("voice_evidence.json", voices), ("duration_policy.json", {"chain": "B", "phase": "reverse"})):
        tools._write_json(name, value)
    # A pre-existing recording-global speaker report is safe to reuse read-only.
    speaker_file = source_output / "speaker_evidence.json"
    if speaker_file.is_file():
        (output / speaker_file.name).write_bytes(speaker_file.read_bytes())
    scoped_report = {"video": {**info, "assigned_segment": scope}, "sheets": sheets,
                     "voice_evidence": voices,
                     **({"audio_bindings": report["audio_bindings"]} if report.get("audio_bindings") else {}),
                     "coverage": {"assigned_range": scope, "sheets": len(sheets)},
                     "note": "图上标签是原片秒数。区间外至多0.5秒的边界帧仅供衔接，不属于本段内容。"}
    text = ("【B链路子任务，当前只交付一段】\n" + json.dumps(scope, ensure_ascii=False) +
            "\n只写assigned_segment对应的一份完整提示词。其他段由其他子智能体处理，不展开其他段。"
            "正文时间从0开始，总时长不变；每个局部时间=原片时间减去本段start，不得改变原切镜。"
            "生成提示词里的@视频1指本段裁切视频；以下分析工具和抽帧仍使用原片秒数。"
            "不要重复外层‘第几段提示词’标题，程序会按真实段号添加。"
            "\n【所有子任务共享的主AI设定】\n" + json.dumps(shared_context, ensure_ascii=False) +
            "\n共用设定须结合本段证据执行，无法核实的身份或产品参数不能写成确定事实。\n\n" +
            prepared_evidence_text(scoped_report, shots, transcript, slots))
    return output, text, prepared_images(output, scoped_report), scope
