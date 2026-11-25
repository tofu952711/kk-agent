"""Offline-only mock; never calls external models."""
import json,re,time,threading
from http.server import BaseHTTPRequestHandler
REPORT={"model_requests":[]}
REPORT_LOCK=threading.Lock()
INITIAL_IMAGE_COUNTS={}

def user_text(body):
    return "\n".join(block.get("text", "")
                     for message in body.get("messages", []) if message["role"] == "user"
                     for block in (message.get("content", []) if isinstance(message.get("content"), list)
                                   else [{"text": message.get("content", "")}]))

class ModelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        rules = body["messages"][0].get("content", "")
        text = user_text(body)
        tool_results = [m for m in body["messages"] if m["role"] == "tool"]
        images = sum(1 for m in body["messages"] if isinstance(m.get("content"), list)
                     for b in m["content"] if b.get("type") == "image_url")
        calls = None
        dsh_compatible = any(t.get('function', {}).get('name', '').startswith('mcp__video__') for t in body.get('tools', []))
        if rules == "TEST_SEGMENT":
            phase = "segment"
            if not tool_results:
                calls = [{"id": "cut-1", "type": "function", "function": {
                    "name": "set_video_segments", "arguments": json.dumps({"segments": [
                        {"start": 0, "end": 16}, {"start": 16, "end": 33}]})}}]
                answer = ""
            else:
                parsed = json.loads(tool_results[-1]["content"])
                assert parsed["status"] == "ready" and parsed["materialized"]
                answer = "工具已实际裁出两段；继续独立反推。"
        elif "你是B链路的视频反推主AI" in rules:
            phase = "coordinator"
            start = text.index("【已完成的实际分段】\n") + len("【已完成的实际分段】\n")
            segments, _ = json.JSONDecoder().raw_decode(text[start:])
            common = {key: "本地测试，无人物商品及声音，保持色块场景。" for key in (
                "全片内容概要", "人物与服装", "商品与适配", "场景与光线", "声音与说话人", "跨段衔接")}
            common["全片营销策略"] = "非营销色块测试，不新增卖点。"
            common["分段表达计划"] = [{"segment_index": i, "start": s["start"], "end": s["end"],
                                    "本段职责": f"观察第{i}段色块", "重点卖点": "无", "台词推进": "无台词",
                                    "承接与去重": "按原片时间连续"} for i, s in enumerate(segments, 1)]
            answer = json.dumps(common, ensure_ascii=False)
        elif "你是视频分段后的抽帧AI" in rules:
            phase = "frame-selection"
            assert not body.get("tools"), "Frame selector must have no segmentation tools"
            assert "先修改这些图片" in rules
            start = text.index("【当前段真实候选帧】\n") + len("【当前段真实候选帧】\n")
            candidates, _ = json.JSONDecoder().raw_decode(text[start:])
            segment = int(re.search(r"你是第(\d+)段独立抽帧AI", text).group(1))
            # Different frame counts exercise per-segment reset and flat-list order.
            positions = [0]
            chosen = [candidates[i] for i in positions]
            answer = json.dumps({"targets": ["主体"], "reference_bindings": [],
                "shot_analysis":{"mode":"fixed","scene_unchanged":True,"confirmed_changes":[],"reason":"首帧之后单色场景未变"},
                "selected_frames": [{"candidate_id": c["candidate_id"], "visible_targets": ["主体"],
                    "clear_targets": ["主体"], "at_edit_boundary": True,
                    "subject_positions": "色块覆盖画面", "reason": "本地模拟选帧，未做语义质量验证"} for c in chosen],
                "opening_assessment":[{"candidate_id":candidates[0]["candidate_id"],"clear":True,"reason":"测试首帧清晰"}],
                "unmet_targets": [], "notes": "离线测试"}, ensure_ascii=False)
            time.sleep(.25)
        elif rules == "TEST_REVERSE":
            phase = "reverse"
            segment = int(re.search(r"你是第(\d+)段", text).group(1))
            if not tool_results:
                INITIAL_IMAGE_COUNTS[('reverse', segment)] = images
                calls = [{"id": f"frames-{segment}", "type": "function", "function": {
                    "name": "get_frames", "arguments": json.dumps({"times": [.5 if segment == 1 else 16.5]})}}]
                answer = ""
            else:
                assert images > 0, "Tool image was not replayed"
                if dsh_compatible:
                    assert images == INITIAL_IMAGE_COUNTS[('reverse', segment)], "DSH continuation added tool pixels"
                    assert 'Image sha256:' in tool_results[-1]['content'], "Missing DSH attachment description"
                start = text.index("【已导出的抽帧图片与强制引用映射】\n") + len("【已导出的抽帧图片与强制引用映射】\n")
                frames, _ = json.JSONDecoder().raw_decode(text[start:])
                assert all(f['segment_index'] == segment for f in frames), 'another segment frame leaked'
                assert '下一段重新从同样起始编号计算' in text
                answer = f"第{segment}段：按实际视频描述色块，局部时间从0开始。" + ''.join(
                    f"本段{f['local_time']}秒参照编辑后的{f['mention']}保持构图。" for f in frames)
            time.sleep(.25)
        elif "你是B链路最终复核主AI" in rules:
            phase = "review"
            answer = json.dumps({"status": "corrected", "checks": {"一致性": True, "衔接": True},
                                 "issues": ["测试局部替换"], "edits": [{"segment_index": 2,
                                 "old": "按实际视频描述色块", "new": "按实际视频描述蓝色色块",
                                 "reason": "验证现有审核局部替换"}]}, ensure_ascii=False)
        elif rules == "TEST_A":
            phase = "A"
            if not tool_results:
                INITIAL_IMAGE_COUNTS[('A', text)] = images
                calls = [{"id": "inspect-a", "type": "function", "function": {
                    "name": "inspect_video", "arguments": "{}"}},
                         {"id": "frame-a", "type": "function", "function": {
                    "name": "get_frames", "arguments": '{"times":[0.5]}'}}]
                answer = ""
            else:
                assert len(tool_results) == 2
                if dsh_compatible:
                    assert images == INITIAL_IMAGE_COUNTS[('A', text)] and any('Image sha256:' in result['content'] for result in tool_results)
                else:
                    assert images > 0
                answer = "A链路测试通过，保留原规则和工具素材。"
        elif rules == "TEST_BATCH":
            phase = "batch"
            time.sleep(.5)
            answer = text + "完成"
        else:
            raise RuntimeError("Unexpected system rules")
        with REPORT_LOCK:
            REPORT["model_requests"].append({"phase": phase, "image_blocks": images,
                                            "tool_results": len(tool_results)})
        if calls and dsh_compatible:
            available = {t['function']['name'] for t in body.get('tools', [])}
            for call in calls:
                name = call['function']['name']
                if name not in available:
                    call['function']['name'] = 'mcp__video__' + name
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta = {"tool_calls": [dict(index=i, **call) for i, call in enumerate(calls)]} if calls else {"content": answer}
        event = {"choices": [{"index": 0, "delta": delta,
                              "finish_reason": "tool_calls" if calls else "stop"}],
                 "usage": {"prompt_tokens": 100, "completion_tokens": 10}}
        self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode())
        self.wfile.flush()
