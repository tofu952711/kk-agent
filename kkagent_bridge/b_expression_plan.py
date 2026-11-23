"""Validate the coordinator's allocation against the already cut video ranges."""
from __future__ import annotations

import json
import math


PLAN_FIELDS = ("本段职责", "重点卖点", "台词推进", "承接与去重")


def validate_expression_plan(value, segments):
    strategy = value.get("全片营销策略")
    plans = value.get("分段表达计划")
    if not isinstance(strategy, str) or not strategy.strip() or len(strategy) > 2400:
        raise RuntimeError("B主AI缺少有效的全片营销策略；未派发子任务")
    if not isinstance(plans, list) or len(plans) != len(segments):
        raise RuntimeError("B主AI分段表达计划未覆盖全部实际分段；未派发子任务")
    normalized = []
    for index, (plan, segment) in enumerate(zip(plans, segments), 1):
        if not isinstance(plan, dict) or type(plan.get("segment_index")) is not int or plan["segment_index"] != index:
            raise RuntimeError("B主AI分段表达计划段号或顺序不符；未派发子任务")
        for key in ("start", "end"):
            stamp = plan.get(key)
            if (type(stamp) not in (int, float) or not math.isfinite(stamp) or
                    abs(stamp - segment[key]) > 0.000001):
                raise RuntimeError(f"B主AI第{index}段表达计划与实际分段时间不符；未派发子任务")
        if any(not isinstance(plan.get(key), str) or not plan[key].strip() or
               len(plan[key]) > 2400 for key in PLAN_FIELDS):
            raise RuntimeError(f"B主AI第{index}段表达职责不完整；未派发子任务")
        normalized.append({"segment_index": index, "start": segment["start"], "end": segment["end"],
                           **{key: plan[key] for key in PLAN_FIELDS}})
    return {"全片营销策略": strategy, "分段表达计划": normalized}


def segment_assignment_text(contract, index):
    plan = next(item for item in contract["分段表达计划"] if item["segment_index"] == index)
    return ("\n【本段表达职责，按主AI全片计划执行】\n" + json.dumps(plan, ensure_ascii=False) +
            "\n结合全片营销策略及其他段职责，只展开分配给本段的内容；"
            "前段已经完整讲过的卖点和话术不换句话重新讲，后段负责的内容不提前展开。"
            "同一卖点跨段时按计划继续补充新信息、演示或结论，不重新介绍整套商品。"
            "原片本来存在的重复、跨段未说完的句子及用户明确要求保留的台词按原规则处理，"
            "不为去重擅改原话、动作、镜头、切点或时长。人物、商品和声音等共用描述仍完整保留。"
            "营销计划是内部创作依据，成稿按用户模板输出，不附计划或去重说明。")
