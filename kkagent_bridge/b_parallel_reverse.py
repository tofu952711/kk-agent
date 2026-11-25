"""B-only coordinator plus one isolated, concurrent reverse agent per real clip."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import copy
import json
from pathlib import Path
import re
import shutil
import threading
import time

from .b_segment_media import prepare_segment_bundle, scoped_transcript
from .media_workflow import evidence as read_evidence
from .paths import bridge_data_dir
from .video_preparation import prepared_images, prepared_evidence_text
from .workflow import workflow_bridge
from .b_review import REVIEW_RULES, parse_review, apply_review
from .review_ai import resolve_review_ai
from .reference_audio import audio_binding_text
from .b_expression_plan import validate_expression_plan, segment_assignment_text
from .frame_selection import (FRAME_SELECTION_RULES, prepare_candidates, parse_selection,
    materialize_selection, assign_image_numbers, selected_image_payloads,
    frame_reference_text, validate_mentions)

SHARED_FIELDS = ("全片内容概要", "人物与服装", "商品与适配", "场景与光线", "声音与说话人", "跨段衔接")
COORDINATOR_RULES = """你是B链路的视频反推主AI。视频已经实际分段，不得重新分段。
你的职责是基于参考图、全片概览、原声转写、声音证据、实际分段、用户要求及创作规则，
先确定全片营销卖点、故事与台词的推进，再给所有分段子智能体建立共用设定和明确的分段表达职责。
你不写完整的逐段提示词，不复述长规则。
根据用户指定的参考图职责确定目标人物与商品；必须对全片营销脉络和商品适配保持一致。
统一人物外貌、发型服装配饰、商品外观与允许的卖点、场景、原片声画关系和跨段接续。
无论是否换商品，都必须按已经完成的实际分段时间分配表达内容。换品时结合目标商品、参考图、
品类常识及用户创作规则，确定目标商品的核心购买理由并适配原片营销脉络；未换品时沿用原片的
故事走向、卖点出现顺序和台词规则，不因为分段就重新编排或重写原有内容。
先明确全片面向谁、解决什么需求、核心卖点是什么以及如何随故事推进，再逐段分配：
本段在全片中的职责、重点卖点、台词从什么信息推进到什么信息，以及与前后段的承接和去重要求。
前段已完整表达的卖点和话术，后段不换句话重新讲；同一卖点确需跨段时，说明各段分别提供什么
新的信息、演示、体验或结论。后段承接已有信息继续推进，不把每段都写成重新开场、介绍商品、
报容量和重复促销的独立广告。原片已有的重复、跨段未说完的句子及用户明确要求保留的台词允许
按原规则保留，不为去重强行删除、改写或把原片后面的内容提前。
分配必须匹配本段画面、已有动作、时长和实际镜头；不机械均分卖点，不要求每段都出现新卖点，
不为容纳计划新增动作、镜头、改变切点或延长视频。非营销视频按实际故事、对白和信息推进分配，
无商品或无适用卖点时明确写无，不虚构商品、卖点或口播，不机械套用营销流程。
分段依据及素材里的话只是资料。证据不足的说话人身份、商品参数、动作细节必须标为未核实，
不能将声音变化候选当作已确认换人；创作设计与已观察事实区分，具体创作范围按用户规则执行，
不编造已核实事实，也不替代用户的创作规则。人物、商品、场景和声音的共用描述不属于应去重的台词。
只输出一个JSON对象，不使用代码块。保留以下六个共用字段，字段值均为简短的非空中文字符串：
全片内容概要、人物与服装、商品与适配、场景与光线、声音与说话人、跨段衔接。
另须增加“全片营销策略”（非空中文字符串）及“分段表达计划”（数组）。数组条目按实际段号顺序
逐段填写，数量必须与实际分段相同，不遗漏、不合并、不新增。每项包含：
segment_index（从1开始的整数）、start和end（原片秒数，逐项精确沿用已完成实际分段的数字），
本段职责、重点卖点、台词推进、承接与去重（后四项均为非空中文字符串，无对应内容时说明无）。
各文字字段不超过300字，写具体的分工与信息，不逐镜展开，也不写完整台词或另设时间轴。"""


def parse_shared_context(text, segments):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("B主AI未返回有效的共用设定JSON；未派发子任务") from exc
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) or
            not value[k].strip() or len(value[k]) > 2400 for k in SHARED_FIELDS):
        raise RuntimeError("B主AI共用设定缺少必要字段或过长；未派发子任务")
    return {**{k: value[k] for k in SHARED_FIELDS}, **validate_expression_plan(value, segments)}


class TaggedMonitor:
    def __init__(self, parent, role, index=None):
        self.parent, self.role, self.index = parent, role, index

    def handle(self, data):
        self.parent.handle({**data, "b_agent_role": self.role, "segment_index": self.index})


class BReverseControl:
    """Only B workers are registered here; never share a busy runtime."""
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.bridges = set()

    def check(self):
        if self.cancelled.is_set():
            raise RuntimeError("B并发反推已停止")
        if time.monotonic() >= self.deadline:
            raise RuntimeError("B并发反推整体超时，已完成的段落保留在B结果目录")
        # The Comfy interrupt button is checked while the supervisor waits.
        from .cancellation import check_cancelled
        check_cancelled()

    def remaining(self):
        self.check()
        return max(.01, self.deadline - time.monotonic())

    def add(self, bridge):
        with self.lock:
            if self.cancelled.is_set():
                raise RuntimeError("B并发反推已停止")
            self.bridges.add(bridge)

    def remove(self, bridge):
        with self.lock:
            self.bridges.discard(bridge)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            bridges = list(self.bridges)
        # Close all private runtimes together, rather than blocking once per clip.
        if bridges:
            with ThreadPoolExecutor(max_workers=len(bridges)) as pool:
                list(pool.map(lambda b: b.cancel(), bridges))


def await_futures(futures, control):
    """Poll cancellation/deadline, yielding completions rather than input order."""
    pending = set(futures)
    while pending:
        control.check()
        done, pending = wait(pending, timeout=.2, return_when=FIRST_COMPLETED)
        yield from done


def merge_results(results, segments):
    if len(results) != len(segments) or any(not r.get("text", "").strip() for r in results):
        raise RuntimeError("B子任务结果不完整，不能作为全部完成输出")
    parts = []
    for index, (result, segment) in enumerate(zip(results, segments), 1):
        if result.get("segment_index") != index:
            raise RuntimeError("B子任务段号与实际分段顺序不一致")
        parts.append(result["text"].strip())
    return "\n\n\n\n丨\n\n\n\n".join(parts)


def iter_parallel_reverse(*, api_key, base_url, model, timeout_s, max_tokens, api_protocol,
                         reasoning_effort, stream_usage, requirement, reverse_rules,
                         output, report, slots, refs, media_text, segment_answer, video_record,
                         media_mcp, speed, configure_stage, audit_dir, observe=None, observer=None, review_rules=REVIEW_RULES, review_ai=None):
    if not isinstance(review_rules, str) or not review_rules.strip():
        raise ValueError("请填写「链路B：审核规定」")
    reviewer = resolve_review_ai(review_ai, api_key=api_key, base_url=base_url, model=model, api_protocol=api_protocol)
    started = time.monotonic()
    segments = video_record["segments"]
    if len(segments) != video_record["count"] or not 2 <= len(segments) <= 5:
        raise RuntimeError("B并发反推只能使用已经实际裁切成功的2—5段视频")
    result_dir = bridge_data_dir() / "b-results" / speed.run_id
    result_dir.mkdir(parents=True, exist_ok=False)
    control = BReverseControl(timeout_s)
    audits = {}
    artifacts = {}
    results = [None] * len(segments)
    selections = [None] * len(segments)
    speed.phase = "reverse"

    def save(name, value):
        path = result_dir / name
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)

    def status(state, **extra):
        save("status.json", {"chain": "B", "run_id": speed.run_id, "status": state,
                            "segments": segments, "completed_segments": [r["segment_index"] for r in results if r],
                            "elapsed_seconds": round(time.monotonic() - started, 3), **extra})

    def bridge_context():
        return workflow_bridge(api_key, base_url, model, max(10, int(control.remaining())),
                               api_protocol, max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                               stream_usage=stream_usage)

    def coordinator():
        begin = time.monotonic()
        overview = {**report, "sheets": [s for s in report["sheets"] if s["kind"] == "全片概览"]}
        images = refs + prepared_images(output, overview)
        transcript = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
        evidence = ("【已完成的实际分段】\n" + json.dumps(segments, ensure_ascii=False) +
                    "\n【参考视频输出范围；分析仍覆盖完整分段】\n" +
                    json.dumps(video_record.get("reference_videos", []), ensure_ascii=False) +
                    "\n【参考图编号】\n" + json.dumps(slots) +
                    "\n前面的独立参考图依次对应以上image插口；之后为原视频全片概览拼图。\n" +
                    "\n".join(f"概览图片原片秒数：{s['actual_times']}" for s in overview["sheets"]) +
                    "\n【原声转写】\n" + json.dumps(transcript, ensure_ascii=False) +
                    "\n【声音证据，变化候选不等于说话人】\n" + json.dumps(report.get("voice_evidence", {}), ensure_ascii=False) +
                    "\n【分段依据】\n" + segment_answer["text"] +
                    "\n【子智能体须遵守的创作规则资料】\n" + reverse_rules +
                    "\n" + audio_binding_text(report))
        audit = audit_dir / f"{speed.run_id}-b-coordinator.jsonl"
        with bridge_context() as bridge:
            control.add(bridge)
            try:
                configure_stage(bridge, speed, audit, COORDINATOR_RULES, requirement, evidence, "coordinator", images)
                bridge.cfg["startup_timeout_s"] = min(30, control.remaining())
                bridge.cfg["_token_monitor"] = TaggedMonitor(speed, "coordinator")
                answer = bridge.ask(bridge.session_id("chain-b-coordinator"), requirement, images,
                                    timeout=control.remaining(), on_progress=observe)
                contract = parse_shared_context(answer["text"], segments)
                save("shared-context.json", contract)
                return contract, {"seconds": round(time.monotonic() - begin, 3),
                                  "usage": answer.get("usage"), "audit_file": str(audit)}
            finally:
                control.remove(bridge)

    def select_frames(index, segment, contract):
        begin = time.monotonic()
        directory, candidates, sheets = prepare_candidates(output, report, index, segment, control.check)
        images = refs + sheets
        transcript = scoped_transcript(json.loads((output / "transcript.json").read_text(encoding="utf-8")),
                                       segment["start"], segment["end"])
        shots = json.loads((output / "shot_candidates.json").read_text(encoding="utf-8"))
        cut_candidates = [cut for cut in shots.get("candidate_cuts", [])
                          if segment["start"] < cut["time"] < segment["end"]]
        task = requirement + f"\n\n你是第{index}段独立抽帧AI；图片将先交给用户修改，再作为视频生成参考。"
        materials = ("【当前段原片范围】\n" + json.dumps(segment, ensure_ascii=False) +
            "\n【本次上传参考图插口，附件前部依次对应这些插口】\n" + json.dumps(slots) +
            "\n其后为候选拼图，标签Cxxx为候选ID；拼图不计入参考图编号。" +
            "\n【共用设定及素材职责】\n" + json.dumps(contract, ensure_ascii=False) +
            "\n【用户反推规则资料】\n" + reverse_rules +
            "\n【本段原声转写：时间为原片秒数，句末需与画面联合判断】\n" + json.dumps(transcript, ensure_ascii=False) +
            "\n【本段像素切点候选，仅作线索，须以图片确认，不是已确认镜头切换】\n" + json.dumps(cut_candidates, ensure_ascii=False) +
            "\n【当前段真实候选帧】\n" + json.dumps(candidates, ensure_ascii=False))
        audit = audit_dir / f"{speed.run_id}-b-frame-selection-{index}.jsonl"
        # The existing review endpoint is vision-capable. Each selector owns a
        # fresh session; it shares no conversation with reverse or final review.
        with workflow_bridge(**reviewer, timeout_s=max(10, int(control.remaining())),
                             max_tokens=min(max_tokens, 8192), reasoning_effort=reasoning_effort,
                             stream_usage=stream_usage) as bridge:
            control.add(bridge)
            try:
                configure_stage(bridge, speed, audit, FRAME_SELECTION_RULES, task, materials,
                                f"frame-selection-{index}", images)
                bridge.cfg["startup_timeout_s"] = min(30, control.remaining())
                bridge.cfg["_token_monitor"] = TaggedMonitor(speed, "frame-selector", index)
                print(f"[KKGO] B抽帧AI {index}/{len(segments)}：{len(candidates)}张候选，选取可编辑参考帧", flush=True)
                answer = bridge.ask(bridge.session_id(f"chain-b-frame-selection-{index}"), task, images,
                                    timeout=control.remaining(), on_progress=observe)
                (directory / "response.json").write_text(json.dumps({"text": answer["text"]}, ensure_ascii=False), encoding="utf-8")
                try:
                    selection = parse_selection(answer["text"], candidates, slots)
                except ValueError as exc:
                    repair_task = task + f"\n上一稿校验失败：{exc}。重新对照全段候选图片返回完整JSON，在shot_analysis填写scene_unchanged布尔值。首帧之后场景一直未变只选1张段首参考，同场景跳剪或景别变化也不增加图片；只有明确场景切换且提供前后依据才选2—5张，不凑满5张；全部保留清晰度与编辑适用性要求。"
                    bridge.cfg["video_user_prompt"] = repair_task
                    answer = bridge.ask(bridge.session_id(f"chain-b-frame-selection-{index}-repair"),
                        repair_task, images, timeout=control.remaining(), on_progress=observe)
                    (directory / "response-repair.json").write_text(json.dumps({"text": answer["text"]}, ensure_ascii=False), encoding="utf-8")
                    selection = parse_selection(answer["text"], candidates, slots)
                frames = materialize_selection(output.parent / "source-video.mp4", directory,
                    selection, candidates, segment, index, control.check)
                frame_directory = result_dir / "frames" / f"segment-{index}"
                frame_directory.mkdir(parents=True, exist_ok=False)
                for frame in frames:
                    destination = frame_directory / Path(frame["file"]).name
                    shutil.copyfile(frame["file"], destination)
                    frame["file"] = str(destination)
                result = {**selection, "frames": frames, "segment_index": index,
                          "seconds": round(time.monotonic() - begin, 3), "usage": answer.get("usage"),
                          "audit_file": str(audit), "model": reviewer["model"], "candidate_count": len(candidates)}
                save(f"frame-selection-{index}.json", result)
                return result
            finally:
                control.remove(bridge)

    def child(index, segment, contract):
        begin = time.monotonic()
        control.check()
        directory, text, sheets, scope = prepare_segment_bundle(output, report, slots,
            index, segment, len(segments), contract,
            reference=next((r for r in video_record.get("reference_videos", [])
                            if r["segment_index"] == index), None),
            selected_frames=selections[index - 1]["frames"])
        selection = selections[index - 1]
        frames = selection["frames"]
        text += frame_reference_text(frames, slots)
        text += "\n【选帧目标及未覆盖项】\n" + json.dumps({k: selection[k] for k in
            ("targets", "reference_bindings", "unmet_targets", "joint_clear_frame", "opening_frame", "shot_analysis", "notes")}, ensure_ascii=False)
        images = refs + selected_image_payloads(frames) + sheets
        audit = audit_dir / f"{speed.run_id}-b-reverse-{index}.jsonl"
        with bridge_context() as bridge:
            control.add(bridge)
            try:
                mcp = copy.deepcopy(media_mcp)
                args = mcp["args"]
                script_index = next(i for i, arg in enumerate(args) if str(arg).endswith("media_server.py"))
                args[script_index] = str(Path(__file__).with_name("b_media_server.py"))
                args[args.index("--output") + 1] = str(directory)
                bridge.cfg["media_mcp"] = mcp
                bridge.cfg["startup_timeout_s"] = min(30, control.remaining())
                # Keep the user's B system rules literal. Dispatch scope is supplied
                # as user task + separate evidence, surviving exact-input projection.
                task = (requirement + f"\n\n【主AI派发】你是第{index}段反推子智能体。"
                        f"本次只处理原片{segment['start']:.6f}—{segment['end']:.6f}秒这一段，"
                        f"交付一份时长{scope['duration']:.6f}秒、局部时间从0开始的完整提示词。"
                        "规则中的逐段交付在本子任务中仅指这一段，其他段由其他智能体负责。" +
                        segment_assignment_text(contract, index))
                configure_stage(bridge, speed, audit, reverse_rules, task, text, f"reverse-{index}", images)
                bridge.cfg["_empty_response_retries"] = 3
                bridge.cfg["_token_monitor"] = TaggedMonitor(speed, "segment", index)
                print(f"[KKGO] B子智能体{index}/{len(segments)}开始：原片{segment['start']:g}—{segment['end']:g}秒；{len(images)}张图", flush=True)
                answer = bridge.ask(bridge.session_id(f"chain-b-reverse-{index}"), task, images,
                                    timeout=control.remaining(), on_progress=observe)
                if not answer["text"].strip():
                    raise RuntimeError(f"B第{index}段未返回提示词")
                try:
                    validate_mentions(answer["text"], frames, slots)
                except ValueError as exc:
                    # Ask the same child to repair its prose; never silently
                    # append an unrelated attachment list to make validation pass.
                    repair_task = task + f"\n\n上一稿图片引用检查失败：{exc}。请依据抽帧映射重写本段完整提示词，在对应时间与动作正文引用编辑后的图片。"
                    bridge.cfg["video_user_prompt"] = repair_task
                    answer = bridge.ask(bridge.session_id(f"chain-b-reverse-{index}"), repair_task, images,
                                        timeout=control.remaining(), on_progress=observe)
                    validate_mentions(answer["text"], frames, slots)
                result = {"segment_index": index, "segment": segment, "text": answer["text"],
                          "seconds": round(time.monotonic() - begin, 3),
                          "usage": answer.get("usage"), "finish_reason": answer.get("finish_reason"),
                          "tool_calls": [e["data"]["name"] for e in answer["events"] if e.get("type") == "tool/call"],
                          "reference_images": len(refs), "video_sheet_images": len(sheets),
                          "audit_file": str(audit), "media_scope": scope,
                          "selected_frames": frames,
                          "analysis_evidence": read_evidence(directory)}
                save(f"segment-{index}.json", result)
                print(f"[KKGO] B子智能体{index}完成：{result['seconds']}秒", flush=True)
                return result, text, directory
            finally:
                control.remove(bridge)

    def final_review(contract):
        """A real main-agent call after every child succeeds; no silent bypass."""
        begin = time.monotonic()
        speed.phase = "review"
        cuts = [s["end"] for s in segments[:-1]]
        review_sheets = [s for s in report["sheets"] if s["kind"] == "全片概览" or
                        any(abs(cut - candidate) < .1 for cut in cuts
                            for candidate in s.get("candidate_cut_times", []))]
        review_report = {**report, "sheets": review_sheets}
        frames = [frame for selection in selections for frame in selection["frames"]]
        images = refs + selected_image_payloads(frames) + prepared_images(output, review_report)
        shots = json.loads((output / "shot_candidates.json").read_text(encoding="utf-8"))
        transcript = json.loads((output / "transcript.json").read_text(encoding="utf-8"))
        materials = prepared_evidence_text(review_report, shots, transcript, slots, frames)
        payload = {"actual_segments": segments, "shared_context": contract,
                   "reference_videos": video_record.get("reference_videos", []),
                   "note": "按本次审核规定检查全部草稿；检查项与修改范围以审核规定为准。",
                   "drafts": [{"segment_index": r["segment_index"], "segment": r["segment"],
                               "text": r["text"],
                               "verified_evidence": {k: v for k, v in r["analysis_evidence"].items()
                                   if k in ("speaker_evidence.json", "tool_activity")}}
                              for r in results]}
        evidence = ("【原始B反推规则，最终正文必须遵守】\n" + reverse_rules +
                    "\n【主AI最终复核输入：全部草稿和共同约束】\n" + json.dumps(payload, ensure_ascii=False) +
                    "\n【原始素材证据和本次图片编号】\n" + materials +
                    frame_reference_text(frames, slots))
        audit = audit_dir / f"{speed.run_id}-b-review.jsonl"
        task = requirement + "\n\n所有子任务已完成，请逐段复核，按本次内部质检JSON契约返回通过结果或最小修改。"
        with workflow_bridge(**reviewer, timeout_s=max(10, int(control.remaining())),
                             max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                             stream_usage=stream_usage) as bridge:
            control.add(bridge)
            try:
                configure_stage(bridge, speed, audit, review_rules, task, evidence, "review", images)
                bridge.cfg["startup_timeout_s"] = min(30, control.remaining())
                bridge.cfg["_token_monitor"] = TaggedMonitor(speed, "reviewer")
                answer = bridge.ask(bridge.session_id("chain-b-final-review"), task, images,
                                    timeout=control.remaining(), on_progress=observe)
                save("review-response.json", {"text": answer["text"], "usage": answer.get("usage")})
                review = parse_review(answer["text"])
                revised = apply_review(results, review)
                for result, selection in zip(revised, selections):
                    validate_mentions(result["text"], selection["frames"], slots)
                record = {**review, "seconds": round(time.monotonic() - begin, 3),
                          "usage": answer.get("usage"), "audit_file": str(audit),
                          "agent_role": "main_final_review", "segment_count": len(results),
                          "model": reviewer["model"], "api_protocol": reviewer["api_protocol"],
                          "ai_mode": (review_ai or {}).get("review_ai", "沿用当前AI")}
                save("review.json", record)
                for result in revised:
                    save(f"segment-{result['segment_index']}-reviewed.json", result)
                return revised, record
            finally:
                control.remove(bridge)

    pool = ThreadPoolExecutor(max_workers=len(segments), thread_name_prefix="dsh-b-reverse")
    all_futures = []
    try:
        status("coordinating")
        print("[KKGO] B主AI正在统一共用设定、确定全片营销策略并分配各段表达职责", flush=True)
        future = pool.submit(coordinator)
        all_futures.append(future)
        for done in await_futures([future], control):
            contract, main_record = done.result()
        audits["coordinator"] = main_record["audit_file"]
        speed.phase = "frame-selection"
        status("selecting-frames")
        selection_started = time.monotonic()
        frame_futures = {pool.submit(select_frames, i, segment, contract): i
                         for i, segment in enumerate(segments, 1)}
        all_futures.extend(frame_futures)
        for future in await_futures(frame_futures, control):
            index = frame_futures[future]
            selections[index - 1] = future.result()
            audits[f"frame-selection-{index}"] = selections[index - 1]["audit_file"]
        frames = assign_image_numbers(selections, slots)
        selection_seconds = round(time.monotonic() - selection_started, 3)
        for selection in selections:
            save(f"frame-selection-{selection['segment_index']}.json", selection)
        save("frame-manifest.json", {"frames": frames, "selections": selections,
            "upload_reference_slots": slots, "purpose": "edit these frames, then use edited images as video references"})
        status("frames-ready")
        yield {"selected_frames": frames, "frame_selections": selections,
               "frame_selector_count": len(selections), "audits": dict(audits)}
        # The caller can publish the actual IMAGE ports before resuming inference.
        control.deadline = time.monotonic() + timeout_s
        speed.phase = "reverse"
        status("reversing", shared_context_file=str(result_dir / "shared-context.json"))
        parallel_started = time.monotonic()
        futures = {pool.submit(child, i, segment, contract): i for i, segment in enumerate(segments, 1)}
        all_futures.extend(futures)
        failures = {}
        for future in await_futures(futures, control):
            index = futures[future]
            try:
                result, text, directory = future.result()
                results[index - 1] = result
                audits[f"reverse-{index}"] = result["audit_file"]
                artifacts[str(index)] = str(directory)
            except Exception as exc:
                failures[index] = f"{type(exc).__name__}: {exc}"
            status("reversing", failed_segments=failures)
        if failures:
            raise RuntimeError(f"B并发反推未全部完成，失败段号：{','.join(map(str, sorted(failures)))}；"
                               f"已完成结果保留在{result_dir}。" + "；".join(failures.values()))
        parallel_seconds = round(time.monotonic() - parallel_started, 3)
        draft_text = merge_results(results, segments)
        (result_dir / "draft-prompts.txt").write_text(draft_text, encoding="utf-8")
        status("reviewing")
        print("[KKGO] B子任务全部完成，主AI正在按「链路B：审核规定」进行最终审核", flush=True)
        review_future = pool.submit(final_review, contract)
        all_futures.append(review_future)
        for done in await_futures([review_future], control):
            reviewed_results, review_record = done.result()
        audits["review"] = review_record["audit_file"]
        text = merge_results(reviewed_results, segments)
        (result_dir / "prompts.txt").write_text(text, encoding="utf-8")
        record = {"mode": "one_agent_per_materialized_segment", "agent_count": len(segments),
                  "coordinator": main_record, "shared_context": contract,
                  "frame_selector_count": len(selections), "frame_selections": selections,
                  "selected_frames": frames, "frame_selection_wall_seconds": selection_seconds,
                  "segments": reviewed_results, "review": review_record, "result_directory": str(result_dir),
                  "parallel_wall_seconds": parallel_seconds,
                  "total_seconds": round(time.monotonic() - started, 3),
                  "sum_child_seconds": round(sum(r["seconds"] for r in results), 3),
                  "child_evidence_directories": artifacts}
        save("parallel-summary.json", record)
        status("completed")
        return text, record, audits
    except BaseException as exc:
        for future in all_futures:
            future.cancel()
        # Also catch a worker crossing from preparation into ask() just as stop
        # arrives. Keep cancelling until each owned runtime has actually exited.
        while True:
            control.cancel()
            pending = [future for future in all_futures if not future.done()]
            if not pending:
                break
            wait(pending, timeout=.2)
        status("failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def run_parallel_reverse(**kwargs):
    stages = iter_parallel_reverse(**kwargs)
    try:
        next(stages)
        try:
            next(stages)
        except StopIteration as finished:
            return finished.value
        raise RuntimeError("B反推阶段未正确结束")
    finally:
        stages.close()
