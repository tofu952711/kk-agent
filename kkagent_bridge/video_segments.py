"""Validate AI-selected source ranges; no creative rules or model calls."""
from __future__ import annotations

import math


def validate_segments(segments, duration, fps=30):
    if not isinstance(segments, list) or not 1 <= len(segments) <= 5:
        raise ValueError("分段必须为1—5段，按原视频时间顺序连续覆盖全片")
    tolerance = min(0.05, 1 / max(float(fps or 30), 1))
    result = []
    previous = 0.0
    for index, segment in enumerate(segments, 1):
        if not isinstance(segment, dict):
            raise ValueError(f"第{index}段须提供start和end秒数")
        start, end = segment.get("start"), segment.get("end")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
               for v in (start, end)):
            raise ValueError(f"第{index}段的start/end必须是有限秒数")
        if start < 0 or end > duration + tolerance or end <= start:
            raise ValueError(f"第{index}段超出原视频范围或时长非正")
        if abs(start - previous) > 0.000001:
            raise ValueError(f"第{index}段不连续：相邻段必须共享同一切点，不得遗漏或重叠")
        if index == len(segments):
            if abs(end - duration) > tolerance:
                raise ValueError("最后一段必须到原视频结尾，不得遗漏尾部")
            end = float(duration)
        if end - start < min(float(duration), 1 / max(float(fps or 30), 1)) - 0.000001:
            raise ValueError(f"第{index}段不足一帧，请调整分段切点")
        result.append({"start": float(start), "end": float(end)})
        previous = end
    return result


SEGMENT_TOOL = {
    "name": "set_video_segments",
    "description": "按AI指定的start/end秒数实际裁切原视频并返回分段成品。仅在链路B分段阶段使用；必须先遵循本阶段系统提示词判断切点和段数，再调用此工具，不能只用文字描述。提交2—5段按时间顺序连续覆盖全片的区间，不得遗漏或重叠。工具成功返回status=ready及文件后，说明分段依据并结束本阶段；随后独立使用反推规定生成提示词。可修正，采用最后一次成功结果。",
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {"segments": {"type": "array", "minItems": 2, "maxItems": 5,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {"start": {"type": "number", "minimum": 0},
                               "end": {"type": "number", "exclusiveMinimum": 0}},
                "required": ["start", "end"]}}}, "required": ["segments"]},
}
