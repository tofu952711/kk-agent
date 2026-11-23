"""Full VIDEO input to Harness with local analysis tools."""
from __future__ import annotations
import asyncio
from ..kkagent_bridge.cancellation import cancellable_to_thread
import hashlib
import json
from pathlib import Path
import time
from comfy_api.latest import io
from ..kkagent_bridge.workflow import workflow_bridge
from ..kkagent_bridge.media_workflow import configure_media_config, evidence
from ..kkagent_bridge.video_preparation import prepare_video, prepared_images, prepared_evidence_text
from ..kkagent_bridge.images import tensor_to_base64
from ..kkagent_bridge.media_tools import MediaTools
from ..kkagent_bridge.duration_routing import route_duration
from ..kkagent_bridge.b_review import REVIEW_RULES
from ..kkagent_bridge.a_review import REVIEW_RULES as A_REVIEW_RULES


REVERSE_SETTINGS = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "timeout_s": 1032,
    "max_tokens": 65536,
    "asr_model_dir": "",
    "api_protocol": "openai-completions",
    "reasoning_effort": "high",
}


REVIEW_SETTINGS = {
    "base_url": "https://api.zdjapi.com",
    "model": "gpt-6.1-sol",
    "api_protocol": "openai-completions",
}


def fixed_review_config(review_api_key, legacy_review=None, review_base_url=None):
    key = review_api_key
    if not key and isinstance(legacy_review, dict):
        key = legacy_review.get("api_key", "")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("请在审核区域的数值中填写密钥")
    address = review_base_url
    if address is None:
        address = legacy_review.get("base_url") if isinstance(legacy_review, dict) else None
        address = address or REVIEW_SETTINGS["base_url"]
    if not isinstance(address, str) or not address.strip():
        raise ValueError("请在审核区域的数值2中填写地址")
    return {"review_ai": "自定义", "api_key": key, **REVIEW_SETTINGS, "base_url": address.strip()}


class KKGO_FullVideoAgent(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(node_id="KKGO_FullVideoAgent", display_name="KK agent runninghub 完整视频反推", category="KK agent runninghub",
            description="只按视频实际时长选择独立链路：不足32秒进入A，按A规则反推后由独立审核AI检查；32至60秒进入B，AI按B分段规定调用工具实际裁切，再独立使用B反推规定生成提示词。两阶段均携带共用素材资料。超过60秒报错。支持最多五个分段VIDEO输出。",
            hidden=[io.Hidden.unique_id],
            inputs=[io.Video.Input("video", display_name="原视频"),
                    *[io.Image.Input(f"image{index}", optional=True, display_name=f"image{index}",
                                     tooltip=f"通用参考图{index}，用途由本次用户要求和系统提示词指定；可不连接。")
                      for index in range(1, 11)],
                    io.String.Input("api_key", default="", display_name="数值"),
                    io.String.Input("requirement", multiline=True, default="按系统提示词分析原视频；参考图用途在此说明。",
                                    display_name="本次用户要求", tooltip="第一个文本框：本次用户输入。系统提示词请填在第二个文本框。"),
                    io.String.Input("rules_text", optional=True, multiline=True, default="", lazy=True, display_name="系统提示词（原Skill正文）",
                                    placeholder="在此粘贴系统提示词，例如原 SKILL.md 正文；也可转为输入连接文本节点。",
                                    tooltip="第二个文本框：作为模型系统提示词发送。插件不再内置爆款裂变；右键转为输入后可连接外部文本节点。"),
                    io.Boolean.Input("keep_audio", optional=True, default=True, display_name="保留音轨",
                                     tooltip="控制链路B分段工具是否保留原音轨。链路A从video1输出最长30秒参考视频及其音轨，反推保留完整原片信息，导出音频由下游节点控制。"),
                    io.String.Input("segment_rules_text", optional=True, multiline=True, default="", lazy=True,
                                    display_name="链路B：分段规定",
                                    placeholder="链路B：在此填写视频分段规定，AI将按此规定调用工具实际裁切。",
                                    tooltip="仅作为链路B分段阶段的系统规则。AI决定切点并调用工具输出2—5段视频；A不读取。支持连接外部文本节点。"),
                    io.String.Input("chain_b_reverse_rules_text", optional=True, multiline=True, default="", lazy=True,
                                    display_name="链路B：反推提示词规定",
                                    placeholder="链路B：在此填写分段完成后的反推提示词规定。",
                                    tooltip="仅作为链路B反推阶段的系统规则。工具实际裁切成功后，在独立会话中传递分段结果与共用素材；不使用A系统提示词。支持连接外部文本节点。"),
                    io.Boolean.Input('stream_usage', optional=True, default=True, display_name='请求token用量',
                                     tooltip='开启后向Chat Completions接口发送stream_options.include_usage，用于计算tok/s。CodeZ Responses和DeepSeek Messages使用自身用量字段。接口不支持时可关闭；没有用量则不估算速度。'),
                    io.String.Input("chain_b_review_rules_text", optional=True, multiline=True, default=REVIEW_RULES, lazy=True,
                                    display_name="链路B：审核规定",
                                    placeholder="在此编辑B链路最终审核全文；也可右键转为输入连接文本节点。",
                                    tooltip="全部分段反推完成后，按此文本审核并修改。默认显示原审核全文，可自行修改检查项；请保留JSON返回格式说明。A不读取此字段。"),
                    io.String.Input("review_api_key", optional=True, default="", display_name="数值",
                                    tooltip="审核密钥；模型、协议及思考等级已固定。"),
                    io.String.Input("review_base_url", optional=True, default=REVIEW_SETTINGS["base_url"], display_name="数值2"),
                    *[io.Audio.Input(f"audio{index}", optional=True, display_name=f"音频{index}",
                                     tooltip=f"可选：接入后告知AI‘音频{index}绑定模特{index}’。只检测有无输入，不分析音频；A/B均生效。")
                      for index in range(1, 4)],
                    io.String.Input("chain_a_review_rules_text", optional=True, multiline=True, default=A_REVIEW_RULES, lazy=True,
                                    display_name="链路A：审核规定",
                                    placeholder="填写A链路审核提示词；也可右键转为输入连接文本节点。",
                                    tooltip="A反推完成后独立调用审核AI，按此规则审核并修改完整原片草稿。请保留JSON返回格式说明；整篇草稿段号为1。审核AI使用上方审核区域的数值，B不读取此规则字段。")],
            outputs=[io.String.Output(display_name="提示词"), io.String.Output(display_name="分析证据"), io.String.Output(display_name="状态"),
                     *[io.Video.Output(display_name=f"video{index}") for index in range(1, 6)]])

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    def check_lazy_status(cls, video, rules_text=None, segment_rules_text=None, chain_b_reverse_rules_text=None,
                          chain_b_review_rules_text=REVIEW_RULES, chain_a_review_rules_text=A_REVIEW_RULES, **kwargs):
        routing = route_duration(video.get_duration())
        if routing["chain"] == "A":
            return [name for name, value in (("rules_text", rules_text),
                    ("chain_a_review_rules_text", chain_a_review_rules_text)) if value is None]
        return [name for name, value in (("segment_rules_text", segment_rules_text),
                ("chain_b_reverse_rules_text", chain_b_reverse_rules_text),
                ("chain_b_review_rules_text", chain_b_review_rules_text)) if value is None]

    @classmethod
    async def execute(cls, video, api_key, base_url=None, model=None, requirement="",
                      timeout_s=900, max_tokens=32768, asr_model_dir="", _observer=None,
                      api_protocol="deepseek-messages", rules_text="", keep_audio=True, segment_rules_text="", reasoning_effort="high", stream_usage=True,
                      image1=None, image2=None, image3=None, image4=None, image5=None,
                      image6=None, image7=None, image8=None, image9=None, image10=None,
                      chain_b_reverse_rules_text="", chain_b_review_rules_text=REVIEW_RULES, review_ai=None, audio1=None, audio2=None, audio3=None,
                      chain_a_review_rules_text=A_REVIEW_RULES, review_api_key="", review_base_url=None):
        # Ignore provider overrides carried by older API-format workflows.
        base_url, model, timeout_s, max_tokens, asr_model_dir, api_protocol, reasoning_effort = (
            REVERSE_SETTINGS[name] for name in (
                "base_url", "model", "timeout_s", "max_tokens", "asr_model_dir", "api_protocol", "reasoning_effort"))
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("请在本节点的数值中填写密钥")
        review_ai = fixed_review_config(review_api_key, review_ai, review_base_url)
        if video is None:
            raise ValueError("请连接原视频，脚本需要检测时长后选择链路")
        # Dispatch happens before credentials, prompts, references or media staging.
        # Never round duration or inspect prompt wording to choose a chain.
        routing = route_duration(video.get_duration())
        print(f"[KKGO] 视频{routing['duration_seconds']}秒 → 链路{routing['chain']}", flush=True)
        image_values = (image1, image2, image3, image4, image5, image6, image7, image8, image9, image10)
        if routing["chain"] == "B":
            return io.NodeOutput(*await cancellable_to_thread(
                cls.run_chain_b, video, routing, api_key, base_url, model, requirement,
                segment_rules_text, chain_b_reverse_rules_text, timeout_s, max_tokens,
                asr_model_dir, api_protocol, reasoning_effort, stream_usage, keep_audio,
                _observer, image_values, chain_b_review_rules_text, review_ai, (audio1, audio2, audio3)))
        result = await cancellable_to_thread(cls.run_chain_a, video, routing,
            api_key, base_url, model, requirement, rules_text, timeout_s, max_tokens,
            asr_model_dir, api_protocol, reasoning_effort, stream_usage, _observer,
            image_values, (audio1, audio2, audio3), chain_a_review_rules_text, review_ai)
        return io.NodeOutput(*result)

    @classmethod
    def prepare_chain_media(cls, cfg, video, routing, asr_model_dir, image_values, audio_values=(None, None, None)):
        """The default material bundle is identical for A and B."""
        output = configure_media_config(cfg, video, asr_model_dir)
        tools = MediaTools(output.parent / "source-video.mp4", output, Path(cfg["media_model_dir"]))
        tools._write_json("duration_policy.json", {**routing, "phase": "prepared"})
        slots = [index for index, image in enumerate(image_values, 1) if image is not None]
        refs = []
        for index in slots:
            image = image_values[index - 1]
            if len(image.shape) == 4 and image.shape[0] != 1:
                raise ValueError(f"image{index}只接收单张图片；请拆开图片批次后连接独立插口")
            refs.append(tensor_to_base64(image, max_side=1568))
        print(f"[KKGO] 链路{routing['chain']}：正在本地预处理视频和音轨转写", flush=True)
        from ..kkagent_bridge.isolated_preparation import prepare_isolated
        report, shots, transcript = prepare_isolated(output, Path(cfg["media_model_dir"]),
            timeout=cfg.get("request_timeout_s", 900))
        from ..kkagent_bridge.reference_audio import attach_audio_bindings
        attach_audio_bindings(audio_values, output, report)
        media_text = prepared_evidence_text(report, shots, transcript, slots)
        # Both current and future chain requests receive this explicit bundle.
        cfg["video_media_evidence"] = media_text
        return output, report, slots, refs, prepared_images(output, report), media_text

    @classmethod
    def run_chain_b(cls, *args):
        from ..kkagent_bridge.chain_b import run_chain_b
        return run_chain_b(cls, *args)

    @classmethod
    def run_chain_a(cls, video, routing, api_key, base_url, model, requirement,
                    rules_text, timeout_s, max_tokens, asr_model_dir, api_protocol,
                    reasoning_effort, stream_usage, observer, image_values, audio_values=(None, None, None),
                    chain_a_review_rules_text=A_REVIEW_RULES, review_ai=None):
        stages = cls.iter_chain_a(video, routing, api_key, base_url, model, requirement,
            rules_text, timeout_s, max_tokens, asr_model_dir, api_protocol,
            reasoning_effort, stream_usage, observer, image_values, audio_values=audio_values,
            chain_a_review_rules_text=chain_a_review_rules_text, review_ai=review_ai)
        try:
            next(stages)
            try:
                next(stages)
            except StopIteration as finished:
                return finished.value
            raise RuntimeError("链路A反推阶段未正确结束")
        finally:
            stages.close()

    @classmethod
    def iter_chain_a(cls, video, routing, api_key, base_url, model, requirement,
                     rules_text, timeout_s, max_tokens, asr_model_dir, api_protocol,
                     reasoning_effort, stream_usage, observer, image_values, *, publish_video=False, audio_values=(None, None, None),
                     chain_a_review_rules_text=A_REVIEW_RULES, review_ai=None):
        if not isinstance(rules_text, str) or not rules_text.strip():
            raise ValueError("请在第二个文本框「系统提示词（原Skill正文）」填写反推规则，或转为输入连接文本节点")
        if not isinstance(chain_a_review_rules_text, str) or not chain_a_review_rules_text.strip():
            raise ValueError("请填写「链路A：审核规定」，A不会使用B审核规则代替")
        from ..kkagent_bridge.review_ai import resolve_review_ai
        reviewer = resolve_review_ai(review_ai, api_key=api_key, base_url=base_url,
                                     model=model, api_protocol=api_protocol)
        from ..kkagent_bridge.token_speed import SpeedRun
        from comfy_execution.graph_utils import ExecutionBlocker
        started = time.monotonic()
        with SpeedRun(getattr(cls.hidden, 'unique_id', None), model) as speed, workflow_bridge(
                api_key, base_url, model, timeout_s, api_protocol, max_tokens=max_tokens,
                reasoning_effort=reasoning_effort, stream_usage=stream_usage) as bridge:
            from ..kkagent_bridge.paths import bridge_data_dir
            audit_dir = bridge_data_dir() / "request-audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_file = audit_dir / f"{speed.run_id}.jsonl"
            bridge.cfg["_request_audit_file"] = str(audit_file)
            bridge.cfg.update({"_token_monitor": speed, "video_prompt_only": True,
                "video_exact_inputs": True, "video_system_prompt": rules_text,
                "video_user_prompt": requirement})
            output, report, slots, refs, sheets, media_text = cls.prepare_chain_media(
                bridge.cfg, video, routing, asr_model_dir, image_values, audio_values)
            tools = MediaTools(output.parent / "source-video.mp4", output, Path(bridge.cfg["media_model_dir"]))
            tools._write_json("duration_policy.json", {**routing, "phase": "reverse"})
            if observer:
                observer("ready", bridge, output)
            preparation_finished = time.monotonic()
            progress = []
            def observe(event):
                progress.append({"event": event, "phase": speed.phase,
                    "agent_elapsed_seconds": round(time.monotonic() - preparation_finished, 3)})
                if event in ("step/start", "assistant/message", "tool/call", "tool/result", "turn/end"):
                    print(f"[KKGO] {progress[-1]['agent_elapsed_seconds']}s {event}", flush=True)
            # Literal textbox values and the common source evidence stay separate.
            prepared_record = {"stage": "prepared", "run_id": speed.run_id,
                "node_id": str(getattr(cls.hidden, "unique_id", "")), "chain": "A",
                "system_characters": len(rules_text),
                "system_sha256": hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
                "user_characters": len(requirement),
                "user_sha256": hashlib.sha256(requirement.encode("utf-8")).hexdigest(),
                "media_evidence_characters": len(media_text),
                "media_evidence_sha256": hashlib.sha256(media_text.encode("utf-8")).hexdigest(),
                "reference_slots": slots, "reference_images": len(refs),
                "video_sheet_images": len(sheets), "total_images": len(refs) + len(sheets),
                "frame_coverage": report.get("coverage", {})}
            audit_file.write_text(json.dumps(prepared_record, ensure_ascii=False) + "\n", encoding="utf-8")
            # Full-source frames/transcript are prepared before the reference is trimmed.
            from ..kkagent_bridge.a_reference_video import prepare_a_reference
            reference_video, reference_record = prepare_a_reference(video, output, report["video"], timeout_s)
            if reference_record["trimmed"]:
                media_text += ("\n\n【链路A参考视频长度】反推仍覆盖上面完整原片与全部抽帧、音轨证据。"
                    "交给生成模型的参考视频仅保留前30秒，后续原片内容仍须通过本次提示词描述，不因参考视频裁短而删减反推内容。")
                bridge.cfg["video_media_evidence"] = media_text
                prepared_record["media_evidence_characters"] = len(media_text)
                prepared_record["media_evidence_sha256"] = hashlib.sha256(media_text.encode("utf-8")).hexdigest()
                audit_file.write_text(json.dumps(prepared_record, ensure_ascii=False) + "\n", encoding="utf-8")
            # Both A entry node types expose the bounded reference via video1.
            video_outputs = (reference_video, *[ExecutionBlocker(None) for _ in range(4)])
            yield ("", json.dumps({"chain": "A", "chain_status": "prepared",
                   "video_outputs": reference_record}, ensure_ascii=False),
                   "链路A参考视频已就绪（最长30秒）；反推仍分析完整原片", *video_outputs)
            speed.phase = 'reverse'
            before_agent = time.monotonic()
            answer = bridge.ask(bridge.session_id("full-video"), requirement + "\n\n" + media_text, refs + sheets,
                                timeout=timeout_s, on_progress=observe)
            agent_finished = time.monotonic()
            from ..kkagent_bridge.a_review import final_review
            print("[KKGO] A反推完成，正在按「链路A：审核规定」进行最终审核", flush=True)
            final_text, review_record = final_review(draft=answer["text"], requirement=requirement,
                reverse_rules=rules_text, review_rules=chain_a_review_rules_text, media_text=media_text,
                analysis_evidence=evidence(output), images=refs + sheets, reviewer=reviewer,
                ai_mode=(review_ai or {}).get("review_ai", "沿用当前AI"), speed=speed, audit_dir=audit_dir,
                result_dir=bridge_data_dir() / "a-results" / speed.run_id,
                timeout_s=timeout_s - (time.monotonic() - before_agent), max_tokens=max_tokens,
                reasoning_effort=reasoning_effort, stream_usage=stream_usage, on_progress=observe)
            compliance = {"input_mode": "exact_two_textboxes_with_media_evidence", "review_turns": 1,
                "review_seconds": review_record["seconds"], "rules_characters": len(rules_text),
                "rules_sha256": hashlib.sha256(rules_text.encode("utf-8")).hexdigest(),
                "review_rules_sha256": hashlib.sha256(chain_a_review_rules_text.encode("utf-8")).hexdigest(),
                "format_check": "passed", "semantic_check": "passed", "review": review_record}
            records = evidence(output)
            records["chain"] = "A"
            records["chain_status"] = "completed"
            records["request_audit_file"] = str(audit_file)
            records["reference_image_slots"] = slots
            records["media_input"] = {"prepared": True, "sent_to_ai": True,
                "text": media_text, "reference_images": len(refs), "video_sheet_images": len(sheets)}
            records["video_outputs"] = reference_record
            records["output_compliance"] = compliance
            records["reasoning_effort"] = {"selected": reasoning_effort,
                "requested": None if reasoning_effort == "auto" else reasoning_effort}
            records["segment_corrections"] = 0
            records["segment_agent_tool_calls"] = []
            records["segment_usage"] = None
            records["agent_tool_calls"] = [e["data"]["name"] for e in answer["events"] if e.get("type") == "tool/call"]
            records["finish_reason"] = answer.get("finish_reason")
            records["usage"] = answer.get("usage")
            records["review_usage"] = review_record["usage"]
            records["usage_by_stage"] = {"reverse": answer.get("usage"), "review": review_record["usage"]}
            records['token_speed'] = {**speed.snapshot(), 'status': 'completed'}
            records["agent_progress"] = progress
            records["usage_by_round"] = [(e.get("data") or {}).get("usage") or {} for e in answer["events"]
                                         if e.get("type") == "assistant/message"]
            records["timings"] = {"before_agent_seconds": round(before_agent - started, 3),
                "agent_seconds": round(agent_finished - before_agent, 3),
                "output_review_seconds": review_record["seconds"],
                "preparation_seconds": round(preparation_finished - started, 3),
                "segment_planning_seconds": 0.0, "video_output_seconds": 0.0,
                "video_encoding_seconds": round(sum(reference_record["encoding_seconds"]), 3), "video_agent_overlap_seconds": 0,
                "total_seconds": round(time.monotonic() - started, 3)}
            status = "ok" if final_text.strip() else "未返回提示词"
            if observer:
                observer("finished", bridge, output)
            return (final_text, json.dumps(records, ensure_ascii=False, indent=2), status,
                    *video_outputs)
