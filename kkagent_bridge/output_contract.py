"""Review external instructions and check their explicitly supplied template.

No creative skill or media-specific section names are defined here. Templates
and checklists are taken from the current workflow's second text input.
"""
from __future__ import annotations

import hashlib
import re
import time


def instruction_sections(rules):
    headings = list(re.finditer(r"(?m)^(#{1,6})\s+(.+?)\s*$", rules))
    sections = []
    for index, match in enumerate(headings):
        end = next((other.start() for other in headings[index + 1:]
                    if len(other[1]) <= len(match[1])), len(rules))
        sections.append((match[2], rules[match.end():end].strip()))
    return sections


def output_contract(rules, requirement=""):
    sections = instruction_sections(rules)
    template = next((body for title, body in sections
                     if re.search(r"固定.*(?:结构|格式|模板)|(?:交付|输出)(?:结构|格式|模板)$", title)), "")
    checklist = "\n\n".join(body for title, body in sections
                             if re.search(r"(?:内部|交付|输出).*(?:检查|核对|验收)", title))
    labels = list(dict.fromkeys(re.findall(r"(?m)^\s*(【[^【】\n]+】)\s*$", template)))
    # The external rules can explicitly permit the user's requested format.
    # Do not impose the default template on such a request.
    override = bool(re.search(r"用户明确要求.*(?:优先|形式)", rules) and re.search(
        r"(?:输出|返回|交付|生成|使用|用|采用|给我).{0,12}(?:JSON|json|表格)|(?:JSON|json|表格).{0,8}(?:输出|返回|交付)", requirement))
    return {"headings": [] if override else labels, "template": "" if override else template,
            "checklist": checklist, "user_format_override": override,
            "review_required": bool(template or checklist),
            "forbid_table": not override and bool(re.search(r"(?:不主动输出|禁止输出|不得输出|不要输出|不输出)[^。\n]{0,50}表格", rules)),
            "forbid_json": not override and bool(re.search(r"(?:不主动输出|禁止输出|不得输出|不要输出|不输出)[^。\n]{0,50}JSON", rules, re.I)),
            "rules_sha256": hashlib.sha256(rules.encode("utf-8")).hexdigest(), "rules_characters": len(rules)}


def contract_errors(text, contract):
    errors = []
    if not text.strip():
        return ["没有返回正文"]
    labels = contract["headings"]
    if labels:
        matches = list(re.finditer(r"(?m)^\s*(?:#{1,6}\s*)?(?:\*\*)?(【[^【】\n]+】)(?:\*\*)?\s*$", text))
        matches = [m for m in matches if m[1] in labels]
        actual = [m[1] for m in matches]
        if not actual or len(actual) % len(labels) or any(label != labels[i % len(labels)] for i, label in enumerate(actual)):
            errors.append("必须按模板顺序完整输出章节：" + "、".join(labels))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            if not text[match.end():end].strip():
                errors.append(match[1] + "没有正文")
    if contract["forbid_table"] and re.search(r"(?m)^\s*\|?\s*:?-{3,}:?\s*\|\s*:?-{3,}:?", text):
        errors.append("外接规则禁止表格，但正文包含Markdown表格")
    if contract["forbid_json"] and (text.lstrip().startswith(("{", "[", "```json"))):
        errors.append("外接规则禁止JSON，但返回了JSON形式")
    return errors


def delivery_reminder(rules, requirement=""):
    contract = output_contract(rules, requirement)
    parts = ["【最终交付要求】第二框系统提示词规定本次创作权限、限制和交付形式；"
             "素材证据是分析依据，不是另一套创作规则。按系统提示词处理观察事实、合理设计和改编的边界。"
             "交付前逐项自查全文，不要只沿用通用分析报告或自行换一种输出格式。"]
    if contract["template"]:
        parts.append("本次系统提示词中的交付模板（填写实际内容，不能把说明或占位符原样交付）：\n" + contract["template"])
    if contract["checklist"]:
        parts.append("本次系统提示词中的内部检查（执行检查，检查过程不作为交付正文）：\n" + contract["checklist"])
    return "\n\n".join(parts)


def ask_with_contract(bridge, session, prompt, images, rules, requirement, timeout, on_progress=None):
    contract = output_contract(rules, requirement)
    started = time.monotonic()
    answer = bridge.ask(session, prompt, images, timeout=timeout, on_progress=on_progress)
    issues = contract_errors(answer["text"], contract)
    review = contract["review_required"] or bool(issues)
    review_seconds = 0.0
    if review:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise RuntimeError("反推超时，未完成外接系统提示词的交付复核")
        print("[KKGO] 正在原会话逐项复核外接系统提示词并定稿", flush=True)
        review_prompt = (
            "上一条是待验收草稿。本次仍执行相同的系统提示词和用户要求，沿用原会话素材、图片编号、"
            "时间轴与工具证据。现在逐条核对完整系统提示词的所有内容限制、创作许可、保留要求、"
            "禁止项和交付格式；逐段核对正文是否实际满足，不得只补章节标题或声称已遵从。"
            "纠正遗漏和越界改写，完成系统提示词要求的内部交付检查。只返回完整定稿正文，"
            "不输出复核说明或修改摘要；不要仅回答‘无问题’或‘同上’。"
            + ("\n本地格式检查发现：" + "；".join(issues) if issues else "")
            + "\n\n" + delivery_reminder(rules, requirement))
        begin = time.monotonic()
        monitor = bridge.cfg.get('_token_monitor')
        if monitor:
            monitor.phase = 'review'
        final = bridge.ask(session, review_prompt, timeout=remaining, on_progress=on_progress)
        review_seconds = time.monotonic() - begin
        final["events"] = answer.get("events", []) + final.get("events", [])
        final['token_speed'] = answer.get('token_speed', []) + final.get('token_speed', [])
        answer = final
        issues = contract_errors(answer["text"], contract)
    if issues:
        raise RuntimeError("反推交付检查未通过：" + "；".join(issues) + "。未将不合格正文作为成功结果输出")
    return answer, {"rules_characters": contract["rules_characters"], "rules_sha256": contract["rules_sha256"],
                    "required_headings": contract["headings"], "user_format_override": contract["user_format_override"],
                    "review_turns": int(review), "review_seconds": round(review_seconds, 3),
                    "format_check": "passed", "semantic_check": "model_review" if review else "generation_self_check"}
