"""Independent B stages: AI cuts through a tool, then AI reverses with new rules."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import time

from .media_tools import MediaTools
from .media_workflow import evidence
from .token_speed import SpeedRun
from .video_segments import validate_segments
from .workflow import workflow_bridge
from .b_review import REVIEW_RULES
from .review_ai import resolve_review_ai
from .a_reference_video import prepare_reference_video


def read_tool_videos(output, info, timeout=900):
    """Keep complete tool ranges for analysis; bound downstream references to 30s."""
    from comfy_api.latest import InputImpl
    from comfy_execution.graph_utils import ExecutionBlocker
    path = output / "video_segments.json"
    if not path.is_file():
        raise RuntimeError("链路B分段失败：AI未成功调用set_video_segments裁切视频；未开始反推")
    plan = json.loads(path.read_text(encoding="utf-8"))
    ranges = validate_segments(plan.get("segments"), info["duration_seconds"], info["fps"])
    files = plan.get("files", [])
    if (not 2 <= len(ranges) <= 5 or plan.get("status") != "ready" or
            plan.get("executor") != "ai_invoked_media_tool" or not plan.get("materialized") or
            len(files) != len(ranges)):
        raise RuntimeError("链路B分段失败：工具未返回完整的分段视频成品；未开始反推")
    videos, references, analysis_durations = [], [], []
    started = time.monotonic()
    for index, (name, segment) in enumerate(zip(files, ranges), 1):
        target = (output / name).resolve(strict=True)
        if not target.is_relative_to(output.resolve()) or not target.is_file():
            raise RuntimeError("链路B分段工具返回了无效文件")
        video = InputImpl.VideoFromFile(io.BytesIO(target.read_bytes()))
        actual = float(video.get_duration())
        analysis_durations.append(actual)
        clip_info = {**info, "duration_seconds": actual,
                     "audio_tracks": int(bool(plan.get("keep_audio", True) and info.get("audio_tracks")))}
        reference, record = prepare_reference_video(video, output / "reference-videos" / str(index),
            clip_info, timeout - (time.monotonic() - started), source=target, chain="B")
        reference_end = segment["start"] + min(segment["end"] - segment["start"],
            record.get("reference_end_seconds", record["actual_duration_seconds"]))
        references.append({**record, "segment_index": index, "analysis_start": segment["start"],
            "analysis_end": segment["end"], "reference_source_start": segment["start"],
            "reference_source_end": reference_end})
        videos.append(reference)
    plan["storage"] = "in-memory references capped at 30s; complete tool files retained for analysis"
    plan["actual_durations"] = [float(video.get_duration()) for video in videos]
    plan["analysis_actual_durations"] = analysis_durations
    plan["reference_limit_seconds"] = 30.0
    plan["reference_videos"] = references
    videos.extend(ExecutionBlocker(None) for _ in range(5 - len(videos)))
    return videos, plan


def configure_stage(bridge, speed, audit_file, system, user, media_text, phase, images):
    bridge.cfg.update({"_token_monitor": speed, "_request_audit_file": str(audit_file),
        "video_prompt_only": True, "video_exact_inputs": True,
        "video_system_prompt": system, "video_user_prompt": user,
        "video_media_evidence": media_text})
    sha = lambda text: hashlib.sha256(text.encode("utf-8")).hexdigest()
    audit_file.write_text(json.dumps({"stage": "prepared", "chain": "B", "phase": phase,
        "run_id": speed.run_id, "node_id": speed.node_id,
        "system_characters": len(system), "system_sha256": sha(system),
        "user_characters": len(user), "user_sha256": sha(user),
        "media_evidence_characters": len(media_text), "media_evidence_sha256": sha(media_text),
        "total_images": len(images)}, ensure_ascii=False) + "\n", encoding="utf-8")


def iter_chain_b(node, video, routing, api_key, base_url, model, requirement,
                segment_rules, reverse_rules, timeout_s, max_tokens, asr_model_dir,
                api_protocol, reasoning_effort, stream_usage, keep_audio, observer, image_values, review_rules=REVIEW_RULES, review_ai=None, audio_values=(None, None, None)):
    # Validate B's own rule fields before any preprocessing or API work.
    for value, label in ((segment_rules, "链路B：分段规定"), (reverse_rules, "链路B：反推提示词规定"),
                         (review_rules, "链路B：审核规定")):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"请填写「{label}」，链路B不会使用链路A的系统提示词代替")
    resolve_review_ai(review_ai, api_key=api_key, base_url=base_url, model=model, api_protocol=api_protocol)
    from .paths import bridge_data_dir
    started = time.monotonic()
    progress = []
    with SpeedRun(getattr(node.hidden, "unique_id", None), model) as speed, workflow_bridge(
            api_key, base_url, model, timeout_s, api_protocol, max_tokens=max_tokens,
            reasoning_effort=reasoning_effort, stream_usage=stream_usage) as segment_bridge:
        audit_dir = bridge_data_dir() / "request-audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        audits = {phase: audit_dir / f"{speed.run_id}-b-{phase}.jsonl" for phase in ("segment", "reverse")}
        from .b_video_preparation import prepare_b_chain_media
        output, report, slots, refs, sheets, media_text = prepare_b_chain_media(
            segment_bridge.cfg, video, routing, asr_model_dir, image_values, audio_values)
        tools = MediaTools(output.parent / "source-video.mp4", output, Path(segment_bridge.cfg["media_model_dir"]))
        tools._write_json("duration_policy.json", {**routing, "phase": "segment",
            "keep_audio": bool(keep_audio), "tool_timeout_s": timeout_s})
        segment_bridge.cfg["media_mcp"]["toolCallTimeoutMs"] = max(180000, timeout_s * 1000)
        images = refs + sheets
        segment_media = media_text + "\n\n当前为链路B分段阶段。请按本阶段系统提示词判断切点，调用set_video_segments实际裁切视频；只写分段文字不算完成。"
        configure_stage(segment_bridge, speed, audits["segment"], segment_rules,
                        requirement, segment_media, "segment", images)
        if observer:
            observer("ready", segment_bridge, output)
        prepared_at = time.monotonic()

        def observe(event):
            progress.append({"event": event, "phase": speed.phase,
                "agent_elapsed_seconds": round(time.monotonic() - prepared_at, 3)})
            if event in ("step/start", "assistant/message", "tool/call", "tool/result", "turn/end"):
                print(f"[KKGO] 链路B {speed.phase} {progress[-1]['agent_elapsed_seconds']}s {event}", flush=True)

        speed.phase = "segment"
        print("[KKGO] 链路B：AI正在按分段规定调用工具裁切视频", flush=True)
        segment_answer = segment_bridge.ask(segment_bridge.session_id("chain-b-segment"),
            requirement + "\n\n" + segment_media, images, timeout=timeout_s, on_progress=observe)
        videos, video_record = read_tool_videos(output, report["video"],
            timeout=timeout_s - (time.monotonic() - prepared_at))
        segmented_at = time.monotonic()
        stage_records = evidence(output)
        stage_records.update({"chain": "B", "chain_status": "segmented", "ai_called": True,
            "video_outputs": video_record, "reference_image_slots": slots,
            "request_audit_files": {k: str(v) for k, v in audits.items()}})
        # Close the segmentation agent before starting a fresh agent configured
        # with B reverse rules. Its private job files remain alive in this scope.
        segment_bridge.close()
        tools._write_json("duration_policy.json", {**routing, "phase": "reverse",
            "count": video_record["count"], "segments": video_record["segments"]})
        reverse_media = media_text + "\n\n已由AI调用工具完成的链路B分段结果（所有start/end均为原视频秒数；各段局部秒数从0起算）：\n" + json.dumps(
            [{"video": f"video{i}", **item} for i, item in enumerate(video_record["segments"], 1)], ensure_ascii=False)
        if segment_answer["text"].strip():
            reverse_media += "\n分段阶段返回的依据（分析资料）：\n" + segment_answer["text"]
        if observer:
            observer("segmented", segment_bridge, output)
        from .b_parallel_reverse import iter_parallel_reverse
        from .frame_selection import load_output_images
        reverse_started = time.monotonic()
        reverse_stages = iter_parallel_reverse(
            api_key=api_key, base_url=base_url, model=model, timeout_s=timeout_s,
            max_tokens=max_tokens, api_protocol=api_protocol, reasoning_effort=reasoning_effort,
            stream_usage=stream_usage, requirement=requirement, reverse_rules=reverse_rules, review_rules=review_rules, review_ai=review_ai,
            output=output, report=report, slots=slots, refs=refs, media_text=media_text,
            segment_answer=segment_answer, video_record=video_record,
            media_mcp=segment_bridge.cfg["media_mcp"], speed=speed,
            configure_stage=configure_stage, audit_dir=audit_dir, observe=observe)
        try:
            prepared_frames = next(reverse_stages)
            frame_manifest = {"upload_reference_slots": slots,
                "numbering": "每段独立生成：上传图在前，抽帧在后，各段重新编号；output_list_index为总列表位置。",
                "purpose": "先修改抽帧，再将修改后的图按mention编号作为视频生成参考",
                "frames": prepared_frames["selected_frames"],
                "segments": [{k: item[k] for k in ("segment_index", "targets", "reference_bindings",
                    "joint_clear_frame", "unmet_targets", "opening_frame", "opening_assessment", "shot_analysis", "notes")}
                    for item in prepared_frames["frame_selections"]]}
            selected_images = load_output_images(prepared_frames["selected_frames"])
            manifest_text = json.dumps(frame_manifest, ensure_ascii=False, indent=2)
            stage_records.update({"chain_status": "frames-ready", "frame_manifest": frame_manifest})
            print(f"[KKGO] B抽帧已就绪：{len(selected_images)}张图片先交给入口输出，反推尚未开始", flush=True)
            yield ("", json.dumps(stage_records, ensure_ascii=False, indent=2),
                   f"链路B已分段并抽出{len(selected_images)}张图；图片可先保存/编辑，提示词由独立反推节点输出",
                   *videos, selected_images, manifest_text)
            try:
                next(reverse_stages)
            except StopIteration as finished_stage:
                text, parallel, reverse_audits = finished_stage.value
            else:
                raise RuntimeError("B反推阶段未正确结束")
        finally:
            reverse_stages.close()
        audits.pop("reverse", None)
        audits.update({key: Path(value) for key, value in reverse_audits.items()})
        finished = time.monotonic()
        calls = lambda result: [e["data"]["name"] for e in result["events"] if e.get("type") == "tool/call"]
        records = evidence(output)
        records.update({"chain": "B", "chain_status": "completed", "ai_called": True,
            "request_audit_files": {k: str(v) for k, v in audits.items()},
            "reference_image_slots": slots,
            "media_input": {"prepared": True, "sent_to_ai": True, "text": media_text,
                "reference_images": len(refs), "video_sheet_images": len(sheets)},
            "stage_media_evidence": {"segment": segment_media, "reverse": reverse_media},
            "video_outputs": video_record, "parallel_reverse": parallel,
            "segment_agent_tool_calls": calls(segment_answer),
            "agent_tool_calls": [call for child in parallel["segments"] for call in child["tool_calls"]],
            "segment_usage": segment_answer.get("usage"),
            "usage": {"coordinator": parallel["coordinator"]["usage"],
                      "review": parallel["review"]["usage"],
                      "segments": [child["usage"] for child in parallel["segments"]]},
            "finish_reason": "completed", "agent_progress": progress,
            "reasoning_effort": {"selected": reasoning_effort,
                "requested": None if reasoning_effort == "auto" else reasoning_effort},
            "token_speed": {**speed.snapshot(), "status": "completed"},
            "rules": {phase: {"characters": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
                for phase, value in (("segment", segment_rules), ("reverse", reverse_rules), ("review", review_rules))},
            "timings": {"preparation_seconds": round(prepared_at - started, 3),
                "segment_agent_and_tools_seconds": round(segmented_at - prepared_at, 3),
                "video_encoding_seconds": round(sum(video_record["encoding_seconds"]), 3),
                "reverse_coordinator_seconds": parallel["coordinator"]["seconds"],
                "reverse_parallel_seconds": parallel["parallel_wall_seconds"],
                "reverse_review_seconds": parallel["review"]["seconds"],
                "reverse_agent_seconds": round(finished - reverse_started, 3),
                "total_seconds": round(finished - started, 3)}})
        if observer:
            observer("finished", segment_bridge, output)
        return (text, json.dumps(records, ensure_ascii=False, indent=2),
                f"ok：链路B已分段{video_record['count']}段，{parallel['frame_selector_count']}个抽帧AI选出{len(selected_images)}张可编辑参考图，{parallel['agent_count']}个子智能体反推，最终复核通过",
                *videos, selected_images, manifest_text)



def run_chain_b(*args, **kwargs):
    """Compatibility path for existing one-node sequential workflows."""
    stages = iter_chain_b(*args, **kwargs)
    try:
        next(stages)
        try:
            next(stages)
        except StopIteration as finished:
            return finished.value
        raise RuntimeError("链路B反推阶段未正确结束")
    finally:
        stages.close()
