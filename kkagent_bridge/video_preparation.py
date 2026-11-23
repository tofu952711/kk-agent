"""Deterministic evidence first, followed by the official Harness agent.

Only job-local files are reused. No model calls, credentials, creative rules,
cross-user caches or generated frames belong in this module.
"""
from __future__ import annotations

import base64
import io
import json
import math
from pathlib import Path
import time

from .media_tools import MediaTools
from .reference_audio import audio_binding_text


def sampling_plan(info, shots):
    """Bounded source-frame observations, including dense within-shot motion.

    Four cut samples use frame offsets so the two before-cut images cannot
    accidentally round forward across a cut at ordinary frame rates.
    """
    duration = info["duration_seconds"]
    fps = info["fps"] or 30
    last = max(0, duration - 1 / fps)
    overview_step = max(1.0, duration / 60)
    dense_step = max(.25, duration / 240)

    def uniform(step):
        return sorted(set([min(i * step, last) for i in range(math.ceil(duration / step))] + [last]))

    overview, dense = uniform(overview_step), uniform(dense_step)
    cuts = shots["candidate_cuts"]
    selected = sorted(sorted(cuts, key=lambda c: c["score"], reverse=True)[:48], key=lambda c: c["time"])
    cut_context = [max(0, min(last, cut["time"] + offset / fps))
                   for cut in selected for offset in (-2, -1, 0, 3)]
    short = [(i["start"] + i["end"]) / 2 for i in shots["candidate_intervals"]
             if i["end"] - i["start"] < dense_step and not any(i["start"] <= t < i["end"] for t in dense)]
    return {"overview_step": overview_step, "dense_step": dense_step, "overview": overview,
            "dense": dense, "cut_context": cut_context, "short": short[:48], "selected": selected}


def voice_context(voice, transcript, duration, fps=30):
    """Align evidence, without labeling who spoke or whether a mouth moved."""
    tokens, timestamps = transcript.get("tokens", []), transcript.get("timestamps", [])
    for candidate in voice["change_candidates"]:
        t = candidate["time"]
        candidate["transcript_context"] = {
            "before": "".join(str(token) for token, ts in zip(tokens, timestamps) if t - 2 <= ts < t),
            "after": "".join(str(token) for token, ts in zip(tokens, timestamps) if t <= ts < t + 2),
            "note": "按ASR估计token时间截取的上下文，边界不代表准确的说话人交接。"}
    # Preview only the three strongest candidates; all others remain in text
    # with timestamps and can be investigated using the MCP tools.
    focus = sorted(voice["change_candidates"], key=lambda c: c["score"], reverse=True)[:3]
    focus.sort(key=lambda c: c["time"])
    voice["visual_preview_candidate_times"] = [c["time"] for c in focus]
    last = max(0, duration - 1 / (fps or 30))
    return [(c["time"], sorted(set(max(0, min(last, c["time"] - .6875 + i / 8)) for i in range(12))))
            for c in focus]


def prepare_video(output: Path, model_dir: Path | None = None):
    started = time.monotonic()
    tools = MediaTools(output.parent / "source-video.mp4", output, model_dir)
    timings = {}

    def measured(name, action):
        begin = time.monotonic()
        value = action()
        timings[name] = round(time.monotonic() - begin, 3)
        return value

    info = measured("metadata_seconds", tools.info)
    shots = measured("shot_detection_seconds", tools.shots)
    plan = sampling_plan(info, shots)
    overview, dense, contexts, short = (plan[k] for k in ("overview", "dense", "cut_context", "short"))
    cuts = shots["candidate_cuts"]
    selected = plan["selected"]
    all_times = sorted(set(overview + dense + contexts + short))
    measured("frame_decode_seconds", lambda: tools.cache_frames(all_times, sequential=True))
    sheets = []

    def build_sheets():
        # Keep source images legible: no giant 100-frame image which would
        # discard the additional motion observations when resized upstream.
        for name, label, times, batch, cols in (
                ("overview", "全片概览", overview, 20, 5),
                ("motion", "全片密集动作与运镜对照", dense, 20, 5),
                ("cut-context", "候选切点四帧对照", contexts, 16, 4),
                ("short-intervals", "密集采样间隙内的短镜头候选中点", short, 20, 5)):
            for begin in range(0, len(times), batch):
                meta, _ = tools.sheet(times[begin:begin + batch], prefix=name, cols=cols)
                item = {"kind": label, **meta}
                if name == "cut-context":
                    item["candidate_cut_times"] = [c["time"] for c in selected[begin // 4:(begin + batch) // 4]]
                    item["row_layout"] = "每行一个候选；依次切点前约2帧、前1帧、切点帧、后约3帧；以实际PTS标签为准，片头片尾可能重复。"
                sheets.append(item)
    measured("sheet_composition_seconds", build_sheets)

    def transcribe():
        try:
            return tools.transcribe()
        except (ValueError, ImportError) as exc:
            # Missing optional ASR must be explicit, never invented dialogue.
            result = {"has_audio": bool(info["audio_tracks"]), "status": "unavailable", "full_text": "",
                      "segments": [], "reason": str(exc)}
            tools._write_json("transcript.json", result)
            return result
    transcript = measured("transcription_seconds", transcribe)
    voices = measured("voice_analysis_seconds", tools.analyze_voices)
    focus = voice_context(voices, transcript, info["duration_seconds"], info["fps"])
    def voice_images():
        if not focus:
            return
        tools.cache_frames([t for _, times in focus for t in times], sequential=True)
        for boundary, times in focus:
            meta, _ = tools.sheet(times, cols=4, prefix="voice-context")
            sheets.append({"kind": "声音变化附近口型与人物回应对照", "candidate_voice_time": boundary,
                           "observation_note": "只提供真实帧；未自动识别唇动或声画同步，结合前后连续图判断，不把抽帧当完整听看。", **meta})
    measured("voice_visual_seconds", voice_images)
    tools._write_json("voice_evidence.json", voices)
    timings["total_seconds"] = round(time.monotonic() - started, 3)
    report = {"version": 3, "video": info, "timings": timings, "sheets": sheets, "voice_evidence": voices,
              "coverage": {"overview_step_seconds": plan["overview_step"], "overview_frames": len(overview),
                           "dense_step_seconds": plan["dense_step"], "dense_frames": len(dense),
                           "candidate_cuts": len(cuts), "previewed_candidate_cuts": len(selected),
                           "previewed_cut_times": [c["time"] for c in selected],
                           "cut_context_frames": len(contexts), "short_interval_frames": len(short),
                           "unique_requested_frames": len(all_times),
                           "unique_actual_frames": len({tools._frame_index[tools._time_key(t)]["actual"] for t in all_times}),
                           "voice_review_candidates": len(focus),
                           "voice_review_requested_frames": sum(len(times) for _, times in focus)},
              "note": "只取原视频真实帧，没有插帧、补造帧。密集时间序列用于观察镜头内的走位、转身、手物接触及构图随时间的变化；不能仅凭主体在画面中的位移判断摄影机运动。候选不是已确认切镜，须看四帧对照；抽帧不等于连续观看。"}
    tools._write_json("preparation.json", report)
    return report, shots, transcript


def prepared_images(output: Path, report):
    """Read the already labelled video sheets without adding prompt text."""
    from PIL import Image
    images = []
    for sheet in report["sheets"]:
        with Image.open(output / sheet["file"]) as image:
            data = io.BytesIO()
            image.save(data, "PNG")
            images.append(base64.b64encode(data.getvalue()).decode())
    return images


def prepared_evidence_text(report, shots, transcript, image_slots=None):
    """Default source evidence shared by every chain; no creative/output rules."""
    slots = list(image_slots or [])
    mapping = [f"随请求第{index}张图 = image{slot} = @图片{slot}"
               for index, slot in enumerate(slots, 1)]
    text = [
        "本次素材：@视频1为原视频。图片是通用参考，职责由用户要求及系统提示词确定，不预设人物或商品。",
        "已连接图片插口：" + ("、".join(f"image{slot}" for slot in slots) or "无"),
        "图片对应关系：" + ("；".join(mapping) or "未提供独立参考图，不虚构图片条目。"),
        f"独立参考图之后，从随请求第{len(slots) + 1}张开始均为@视频1带时间标签的抽帧拼图，不属于image1—image10。",
        "本地预处理证据如下；可用get_frames按需查看其他时间或单帧大图。",
        "视频信息：" + json.dumps(report["video"], ensure_ascii=False),
        "采样覆盖：" + json.dumps(report["coverage"], ensure_ascii=False), report["note"],
        "候选切点：" + json.dumps(shots["candidate_cuts"], ensure_ascii=False),
        "音轨自动转写：" + json.dumps(transcript, ensure_ascii=False),
        "声音变化复核证据：" + json.dumps(report.get("voice_evidence", {}), ensure_ascii=False),
        audio_binding_text(report),
    ]
    for index, sheet in enumerate(report["sheets"], len(slots) + 1):
        text.append(f"随请求第{index}张图：{sheet['kind']}；{sheet['layout']}；原视频秒数：" +
                    json.dumps(sheet["actual_times"]))
        if sheet.get("row_layout"):
            text.append(sheet["row_layout"] + "各行对应候选切点（秒）：" + json.dumps(sheet["candidate_cut_times"]))
        if "candidate_voice_time" in sheet:
            text.append(f"该图对应约{sheet['candidate_voice_time']}秒声音变化候选；" + sheet["observation_note"])
    return "\n\n".join(t for t in text if t)


def initial_request(output: Path, report, shots, transcript, requirement="", rules_text="", image_slots=None,
                    *, phase="reverse", output_state=None):
    from PIL import Image
    from .duration_routing import output_notice
    from .output_contract import delivery_reminder
    if not isinstance(rules_text, str) or not rules_text.strip():
        raise ValueError("请填写系统提示词（原Skill正文）中的反推规则")
    # The rules are configured in the system role; this is the user-role payload.
    slots = list(image_slots or [])
    mapping = [f"随请求第{index}张图 = image{slot} = @图片{slot}"
               for index, slot in enumerate(slots, 1)]
    text = ["【本次用户要求】\n" + (requirement.strip() or "按系统提示词分析本次素材。"),
            ("【独立分段阶段】结合本次剧情改编、分镜、参考图及详细分段要求，由AI判断切点，并调用set_video_segments提交两段连续覆盖全片的方案。"
             "不要执行反推提示词，不超过两段。没有成功调用工具就不算完成。" if phase == "segment" else output_notice(output_state)),
            "本次素材：@视频1为原视频。图片是通用参考，职责由用户要求及系统提示词确定，不预设人物或商品。",
            "已连接图片插口：" + ("、".join(f"image{slot}" for slot in slots) or "无"),
            "图片对应关系：" + ("；".join(mapping) or "未提供独立参考图，不虚构图片条目。"),
            f"独立参考图之后，从随请求第{len(slots) + 1}张开始均为@视频1带时间标签的抽帧拼图，不属于image1—image10。",
            "本地预处理证据如下；可用get_frames按需查看其他时间或单帧大图。",
            "视频信息：" + json.dumps(report["video"], ensure_ascii=False),
            "采样覆盖：" + json.dumps(report["coverage"], ensure_ascii=False), report["note"],
            "候选切点：" + json.dumps(shots["candidate_cuts"], ensure_ascii=False),
            "音轨自动转写：" + json.dumps(transcript, ensure_ascii=False),
            "声音变化复核证据：" + json.dumps(report.get("voice_evidence", {}), ensure_ascii=False),
        audio_binding_text(report),
            "声音复核方式：先对照声学变化、前后台词、口型和回应关系，再按本次反推规则判断说话身份与出镜/画外状态。"
            "不能仅因未提供说话人标签就把所有台词强制合成同一主播；也不能把每个音高变化当作新增人物。"
            "判断说话人数或区分多条声线时，优先调用diarize_speakers()取得模型估计、每人发言时间及音高/频谱指标；"
            "该工具CPU按需加载，失败时明确报告不可用；不能拿音高变化候选代替声纹聚类结果。"
            "不确定时可调用analyze_voices(start,end)取得局部声学数据和重新转写，配合get_frames补看口型；"
            "证据仍不足则明确该段身份未核实，不生成确定的单声线结论。"]
    images = []
    for index, sheet in enumerate(report["sheets"], len(slots) + 1):
        text.append(f"随请求第{index}张图：{sheet['kind']}；{sheet['layout']}；原视频秒数：" +
                    json.dumps(sheet["actual_times"]))
        if sheet.get("row_layout"):
            text.append(sheet["row_layout"] + "各行对应候选切点（秒）：" + json.dumps(sheet["candidate_cut_times"]))
        if "candidate_voice_time" in sheet:
            text.append(f"该图对应约{sheet['candidate_voice_time']}秒声音变化候选；" + sheet["observation_note"])
        # Bridge.ask uses PNG MIME for all attached images.
        with Image.open(output / sheet["file"]) as image:
            data = io.BytesIO()
            image.save(data, "PNG")
            images.append(base64.b64encode(data.getvalue()).decode())
    if phase == "reverse":
        text.append(delivery_reminder(rules_text, requirement))
    return "\n\n".join(t for t in text if t), images
