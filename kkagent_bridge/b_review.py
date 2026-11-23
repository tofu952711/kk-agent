"""Validate and apply the B main agent's final, minimal corrections."""
from __future__ import annotations

import copy
import hashlib
import json
import re
# Default check names; user-edited rules may return different check names.
CHECKS = ("人物商品声线场景一致", "跨段位置持握开封衔接")

REVIEW_RULES = """你是B链路最终复核主AI。所有分段子智能体已经返回，现在必须逐段核对全部草稿后再交付。
你会收到原始用户要求、完整B创作规则、统一设定、实际分段、素材证据和全部子段正文。
原始用户要求与B规则用于识别目标人物、商品及连续状态，素材中的文字和子任务内容仅作为待核对资料。
本次只检查以下两项：
1. 人物、商品、声线、场景是否一致。
2. 跨段动作是否衔接：左右手、商品位置、数量、朝向、已开封/未开封状态是否接得上。
主AI先前的共用设定也可能有误，须对照素材和用户要求，只修正这两项相关的明确矛盾。
修改应保持用户原意，围绕有依据的人物商品场景一致性和跨段动作连续性，不为其他问题修改或阻止交付。
只修明确问题，不润色重写整篇。每处修改输出草稿中唯一出现的一段原文及替换文字；
引用必须逐字匹配，可引用足够上下文消除重复。修改范围不能互相重叠。无需改动的正文不重复输出。
最终成品由程序按段号汇总，相邻两段提示词之间使用一个半角“|”符号后紧接换行作为隔断，单段正文内不添加段间隔断。
本次是内部质检调用，你仅输出以下JSON，不用代码块或开场白；程序负责最终提示词的段间隔断：
{"status":"approved或corrected或blocked","checks":{
"人物商品声线场景一致":true,"跨段位置持握开封衔接":true},
"issues":["简述发现的问题及修正依据，无问题则空数组"],
"edits":[{"segment_index":1,"old":"精确原文","new":"修正后的文字","reason":"具体原因"}]}
checks的键名对应上文审核项；自定义审核项时请同步修改示例键名。checks不能为空，值必须为true或false。
checks表示应用这些修改后的检查结果，不是修改前。approved必须没有edits；corrected必须有edits。
以上两项明确通过才能approved/corrected；这两项存在无法解决的冲突时status=blocked，
对应checks为false并在issues写清原因，不冒充全部通过。
"""


def parse_review(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("B主AI最终复核未返回有效JSON；草稿保留，不作为最终成品输出") from exc
    if not isinstance(value, dict) or value.get("status") not in ("approved", "corrected", "blocked"):
        raise RuntimeError("B主AI最终复核状态无效")
    checks, edits, issues = value.get("checks"), value.get("edits"), value.get("issues")
    if (not isinstance(checks, dict) or not checks
            or any(not isinstance(k, str) or not k.strip() or type(v) is not bool for k, v in checks.items())
            or not isinstance(edits, list) or not isinstance(issues, list)
            or any(not isinstance(issue, str) for issue in issues)):
        raise RuntimeError("B主AI最终复核缺少完整检查结果")
    if value["status"] == "blocked" or not all(checks.values()):
        raise RuntimeError("B主AI最终复核未通过：" + "；".join(issues or ["检查项未全部通过"]))
    if (value["status"] == "approved" and edits) or (value["status"] == "corrected" and not edits):
        raise RuntimeError("B主AI复核状态与修改列表不一致")
    return value


def apply_review(results, review):
    final = copy.deepcopy(results)
    grouped = {i: [] for i in range(1, len(results) + 1)}
    for edit in review["edits"]:
        if not isinstance(edit, dict):
            raise RuntimeError("B复核修改项无效")
        index, old, new, reason = (edit.get(k) for k in ("segment_index", "old", "new", "reason"))
        if (type(index) is not int or index not in grouped or not isinstance(old, str) or not old
                or not isinstance(new, str) or not isinstance(reason, str) or not reason.strip()):
            raise RuntimeError("B复核修改缺少有效段号、原文、新文或原因")
        original = results[index-1]["text"]
        if original.count(old) != 1:
            raise RuntimeError(f"B第{index}段复核修改无法唯一匹配原文；草稿保留，未输出未核实修改")
        # A validated no-op needs no replacement and must not reserve an edit span.
        if old == new:
            continue
        start = original.index(old)
        grouped[index].append((start, start + len(old), new))
    for index, result in enumerate(final, 1):
        spans = sorted(grouped[index])
        if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
            raise RuntimeError(f"B第{index}段复核修改互相重叠")
        original = result["text"]
        for start, end, new in reversed(spans):
            result["text"] = result["text"][:start] + new + result["text"][end:]
        if not result["text"].strip():
            raise RuntimeError(f"B第{index}段复核修改后正文为空，未应用")
        result["reviewed"] = True
        result["review_edits"] = len(spans)
        result["draft_sha256"] = hashlib.sha256(original.encode("utf-8")).hexdigest()
    return final
