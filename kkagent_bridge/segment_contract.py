"""Promote explicit video-cut requests to a checked task requirement."""
from __future__ import annotations

import json
import re
import time

try:
    from .video_segments import validate_segments
except ImportError:  # MCP media_server runs as a standalone script.
    from video_segments import validate_segments

_NUM = r"([1-9]\d*|[一二两三四五六七八九十])"
_COUNT = re.compile(r"(?:均分|等分|分割|切割|裁切|分|拆|切)(?:成|为)?\s*" + _NUM + r"\s*(?:个)?[段份]")
_NEGATIVE = re.compile(r"(?:不(?:用|要|需(?:要)?)?|无需|取消|禁止)(?:再|实际|进行|视频)?分段")
_CONDITIONAL = re.compile(r"如果|假如|例如|示例|举例|当.{0,30}时|(?:用户|你).{0,30}(?:要求|需要).{0,20}(?:时|则)")
_GENERIC = re.compile(r"(?:视频|原片).{0,16}(?:分段|切片|裁切).{0,16}(?:保存|输出|导出)|(?:分段|切片|裁切)(?:视频|原片)|分段(?:保存|输出|导出)")


def segment_request(requirement, rules_text):
    """Recognize direct commands, not timeline prose or conditional skill examples.

    User-field overrides are considered first; the system-field remains intact
    in the system role. This is a guard for explicit requests, not an AI cut plan.
    """
    for source, text in (("requirement", requirement), ("rules_text", rules_text)):
        result = None
        fenced = False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            for sentence in re.split(r"[。！？!?；;]", line):
                if _CONDITIONAL.search(sentence):
                    continue
                if _NEGATIVE.search(sentence):
                    result = {"required": False, "count": None, "source": source}
                    continue
                match = _COUNT.search(sentence)
                if match and ("视频" in sentence or "原片" in sentence or
                              re.search(r"保存|输出|导出|素材", sentence)):
                    value = match[1]
                    count = int(value) if value.isascii() and value.isdigit() else {
                        "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                        "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}[value]
                    if not 1 <= count <= 5:
                        raise ValueError("本节点最多输出5段视频，请将明确的分段数量调整为1—5段")
                    result = {"required": True, "count": count, "source": source}
                elif _GENERIC.search(sentence):
                    if re.search(r"分段(?:输出|保存|导出)(?:提示词|台词|文字|文本|章节|时间轴)", sentence):
                        continue
                    if not result or not result["required"]:
                        result = {"required": True, "count": None, "source": source}
        if result is not None:
            return result
    return {"required": False, "count": None, "source": "none"}


def validate_requested_count(segments, request):
    if not request or not request.get("required"):
        return
    count = request.get("count")
    if count is not None and len(segments) != count:
        raise ValueError(f"本次明确要求输出{count}段实际视频，收到{len(segments)}段；请重新提交完整分段方案")
    if count is None and len(segments) < 2:
        raise ValueError("本次要求实际视频分段，不能将完整原视频作为分段结果；请提交2—5段方案")


def request_notice(request):
    if not request["required"]:
        return ""
    count = str(request["count"]) if request["count"] is not None else "2—5"
    origin = "第二个文本框的系统提示词" if request["source"] == "rules_text" else "本次用户要求"
    return (f"【必须执行的视频输出任务】已从{origin}识别到明确分段要求：实际输出{count}段视频。"
            "系统提示词中的任务要求同样必须执行，不能因为第一个文本框没有重复而声称未要求分段。"
            "先确认切点并调用set_video_segments提交方案；后台裁切时继续反推提示词。"
            "文字里的镜头时间轴、口头承诺、只调用声音工具均不等于完成视频分段。")


def plan_problem(output, info, request):
    if not request["required"]:
        return None
    path = output / "video_segments.json"
    if not path.is_file():
        return "模型没有调用set_video_segments提交有效分段方案"
    try:
        segments = validate_segments(json.loads(path.read_text(encoding="utf-8"))["segments"],
                                     info["duration_seconds"], info["fps"])
        validate_requested_count(segments, request)
    except (ValueError, KeyError, TypeError) as exc:
        return str(exc)
    return None


def complete_segment_task(bridge, session, answer, output, info, request, deadline, on_progress):
    """One explicit correction in the same agent session; never rerun media/tools."""
    problem = plan_problem(output, info, request)
    if problem is None:
        return answer, 0
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(f"视频分段未完成：{problem}；本次任务已到时间上限，未返回整片冒充成功")
    print(f"[KKGO] 分段验收未通过：{problem}；正在同一会话补执行一次", flush=True)
    correction = (request_notice(request) + f"\n执行检查未通过：{problem}。\n"
                  "沿用当前会话已有的视频时长、切点、声音和图片证据，不重新开始整个分析。"
                  "现在必须实际调用set_video_segments；工具返回后继续并返回完整最终提示词，"
                  "保留此前正确的反推内容，纠正此前‘未要求分段’等错误描述。"
                  "若无法完成，明确说明原因，不得声称分段成功。")
    repaired = bridge.ask(session, correction, timeout=remaining, on_progress=on_progress)
    problem = plan_problem(output, info, request)
    if problem:
        raise RuntimeError(f"视频分段未完成（已补执行一次）：{problem}；未返回完整原视频冒充成功")
    repaired["events"] = answer["events"] + repaired["events"]
    return repaired, 1
