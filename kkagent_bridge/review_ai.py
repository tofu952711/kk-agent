"""Resolve final-review credentials without changing segmentation or workers."""
from __future__ import annotations

INHERIT = "沿用当前AI"
CUSTOM = "自定义"


def resolve_review_ai(review_ai, *, api_key, base_url, model, api_protocol):
    original = dict(api_key=api_key, base_url=base_url, model=model, api_protocol=api_protocol)
    if review_ai is None:
        return original
    if not isinstance(review_ai, dict):
        raise ValueError("审核AI配置无效，请重新选择沿用当前AI或自定义")
    mode = review_ai.get("review_ai", INHERIT)
    if mode == INHERIT:
        return original
    if mode != CUSTOM:
        raise ValueError("审核AI模式无效，请选择沿用当前AI或自定义")
    for name, label in (("api_key", "API Key"), ("base_url", "Base URL"), ("model", "模型")):
        value = review_ai.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写自定义审核AI的{label}")
    protocol = review_ai.get("api_protocol", "openai-completions")
    if protocol not in ("openai-completions", "deepseek-messages", "codez"):
        raise ValueError("自定义审核AI接口协议无效")
    from .config import validate_updates
    settings = validate_updates(dict(api_key=review_ai["api_key"], base_url=review_ai["base_url"],
                                    model=review_ai["model"], api_protocol=protocol))
    return {name: settings[name] for name in original}
