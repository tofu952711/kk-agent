"""Duration routing is local and cannot be overridden by prompt wording."""
from __future__ import annotations

import math
import json
import time

from .video_segments import validate_segments


def route_duration(duration):
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确定有效视频时长")
    if duration > 60:
        raise ValueError(f"原视频为{duration:g}秒，当前策略仅支持60秒以内的视频")
    chain = "A" if duration < 32 else "B"
    return {"duration_seconds": duration, "chain": chain,
            "split_required": chain == "B", "count": None if chain == "B" else 0,
            "boundary_note": "不足32秒进入链路A；32至60秒进入链路B，由AI调用工具分段后独立反推"}



def output_notice(state):
    if state and state.get("split_required"):
        import json
        return ("【插件已完成实际视频分段】原视频超过31.9秒且不超过60秒，AI已判断切点，插件已按该方案完成两段裁切。"
                "本次处于反推阶段，不允许再调用set_video_segments或修改裁切方案。"
                "现在只按第二个文本框的系统提示词及本次用户要求完成反推提示词。"
                "分段素材与原片秒数对应如下，分析原片时间轴时不要混淆局部秒数：" +
                json.dumps(state["segments"], ensure_ascii=False) +
                "\nAI分段阶段给出的剧情及分镜承接依据（作为分析参考）：\n" + state.get("ai_segment_rationale", ""))
    return ("【插件时长策略】原视频31.9秒及以下，直接执行反推提示词，不执行视频分段。"
            "不生成或保存视频输出，五个VIDEO分支均阻止下游执行。"
            "第三个文本框的分段要求不读取、不发送。本次不提供set_video_segments工具。")


def read_two_segment_plan(output, info):
    path = output / "video_segments.json"
    if not path.is_file():
        raise ValueError("分段阶段未调用set_video_segments提交有效方案")
    ranges = validate_segments(json.loads(path.read_text(encoding="utf-8"))["segments"],
                               info["duration_seconds"], info["fps"])
    if len(ranges) != 2:
        raise ValueError("分段阶段必须提交两段方案")
    return ranges


def ask_segment_plan(bridge, prompt, sheets, output, info, timeout, on_progress):
    session = bridge.session_id("segment-only")
    started = time.monotonic()
    answer = bridge.ask(session, prompt, sheets, timeout=timeout, on_progress=on_progress)
    corrections = 0
    try:
        ranges = read_two_segment_plan(output, info)
    except ValueError as exc:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeError("分段规划超时，未开始反推任务") from exc
        print("[KKGO] 分段方案未提交，正在原分段会话补执行一次", flush=True)
        repaired = bridge.ask(session,
            f"分段验收未通过：{exc}。沿用当前视频证据和详细分段要求，现在必须调用set_video_segments，"
            "提交恰好两段连续覆盖全片的start/end秒数。这里只做分段规划，不生成反推提示词。",
            timeout=remaining, on_progress=on_progress)
        repaired["events"] = answer["events"] + repaired["events"]
        answer, corrections = repaired, 1
        try:
            ranges = read_two_segment_plan(output, info)
        except ValueError as exc:
            raise RuntimeError(f"分段规划失败：{exc}；未开始反推任务") from exc
    return ranges, answer, corrections
