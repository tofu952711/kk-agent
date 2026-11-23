"""Presence-only AUDIO references: never inspect, decode, analyze or upload samples."""
from __future__ import annotations

import json


def audio_bindings(audio_values):
    return [{"input": f"audio{index}", "audio_reference": f"@音频{index}",
             "model_reference": f"模特{index}", "binding": f"音频{index}绑定模特{index}"}
            for index, value in enumerate(audio_values, 1) if value is not None]


def attach_audio_bindings(audio_values, output, report):
    bindings = audio_bindings(audio_values)
    if bindings:
        report["audio_bindings"] = bindings
        (output / "audio_binding_evidence.json").write_text(
            json.dumps({"mode": "presence_only", "bindings": bindings}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (output / "preparation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def audio_binding_text(report):
    bindings = report.get("audio_bindings", [])
    if not bindings:
        return ""
    return ("【用户接入的音频与模特绑定关系】\n" +
            "\n".join(item["binding"] + f"（{item['audio_reference']}）" for item in bindings) +
            "\n只使用以上实际接入的对应关系，编号保持原样，不补写未接入的音频，"
            "不据此新增画面人物或推断图片编号；模特身份以用户要求和人物参考为准。"
            "此信息仅说明音频参考的绑定，不包含音频内容，不代表已听取或分析声音。"
            "不据此编写音色、年龄、性别或音频台词，也不替换原视频对白。"
            "在对应人物的声音参考说明中保留相应的@音频编号绑定关系。")
