"""Offline protocol, isolation, tool safety and cancellation regression tests."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import ssl
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-python-tests-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "results")
sys.path.insert(0, str(ROOT))
from kkagent_bridge import python_backend as backend
from kkagent_bridge.python_provider import Stream, payload, endpoint, transport_error
from aiohttp.client_reqrep import ConnectionKey

CALLS = []
LOCK = threading.Lock()
STARTED = threading.Event()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with LOCK:
            CALLS.append(body)
        model = body["model"]
        if model == "broken-response":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
            self.wfile.flush()
            self.close_connection = True
            return
        if model == "empty-stream-three":
            attempt = sum(call["model"] == model for call in CALLS)
            delta = {"reasoning_content":"internal-only"} if attempt <= 3 else {"content":"stream-recovered"}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"choices":[{"index":0,"delta":delta,"finish_reason":None}]},
                      {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],
                       "usage":{"prompt_tokens":11,"completion_tokens":7}}]
            for event in events:
                self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')
            return
        if model == "blocked":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            STARTED.set()
            time.sleep(2)
            return
        if self.path.endswith("/messages"):
            answer = {"content": [{"type": "text", "text": "messages-ok"}], "stop_reason": "end_turn", "usage": {"input_tokens": 11, "output_tokens": 7}}
            if model == "tool" and not any(b.get("type") == "tool_result" for m in body["messages"] for b in m.get("content", [])):
                answer.update(content=[{"type": "thinking", "thinking": "kept", "signature": "signed"},
                                       {"type": "tool_use", "id": "call-1", "name": "echo", "input": {"value": "ok"}}], stop_reason="tool_use")
        elif self.path.endswith("/responses"):
            answer = {"output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "responses-ok"}]}], "status": "completed", "usage": {"input_tokens": 11, "output_tokens": 7}}
            if model == "tool" and not any(item.get("type") == "function_call_output" for item in body["input"]):
                answer["output"] = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
                                    {"type": "function_call", "id": "fc_1", "call_id": "call-1", "name": "echo", "arguments": '{"value":"ok"}'}]
        else:
            tools = [m for m in body["messages"] if m["role"] == "tool"]
            user = next(m for m in body["messages"] if m["role"] == "user")
            text = user["content"][0]["text"]
            content = {"role": "assistant", "content": "echo:" + text}
            finish = "stop"
            if model in ("tool", "invalid-tool", "duplicate", "unavailable", "tool-empty-once") and len(tools) < (2 if model == "duplicate" else 1):
                name = "mcp__video__echo" if model != "unavailable" else "mcp__video__set_video_segments"
                args = {"value": 1 if model == "invalid-tool" else "ok"}
                content = {"role": "assistant", "content": "", "reasoning_content": "retained reasoning",
                           "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            if model == "empty":
                content["content"] = ""
            if model == "tool-empty-once" and tools:
                attempts = sum(call["model"] == model and any(m["role"] == "tool" for m in call["messages"]) for call in CALLS)
                if attempts == 1:
                    content["content"] = ""
            if model == "truncated-output":
                finish = "length"
            answer = {"choices": [{"message": content, "finish_reason": finish}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(answer).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            pass


class FakeWorker:
    opened = closed = called = 0
    definitions = [{"name": "echo", "description": "echo tool", "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"], "additionalProperties": False}}]
    resources = False

    def __init__(self, cfg):
        pass

    async def __aenter__(self):
        type(self).opened += 1
        return self

    async def rpc(self, name, arguments):
        type(self).called += 1
        return {"content": [{"type": "text", "text": "tool-ok"}]}

    async def close(self):
        type(self).closed += 1


class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        CALLS.clear()
        STARTED.clear()
        FakeWorker.opened = FakeWorker.closed = FakeWorker.called = 0

    def cfg(self, **changes):
        return {"api_key": "offline-placeholder", "base_url": f"http://127.0.0.1:{self.server.server_port}/v1", "model": "echo",
                "api_protocol": "openai-completions", "video_system_prompt": "  literal\n规则正文\n", "video_user_prompt": "my task",
                "video_media_evidence": "separate evidence", "workspace": TEMP.name, "reasoning_effort": "auto", **changes}

    def run_model(self, cfg, blocks=None, timeout=5):
        runtime = backend.SDKRuntime(cfg)
        try:
            return runtime.run("same-logical-session", blocks or [{"type": "text", "text": "derived text must not replace user box"}], timeout)
        finally:
            runtime.stop()

    def test_three_protocols_use_same_literal_rules(self):
        for name, expected in (("openai-completions", "echo:my task"), ("deepseek-messages", "messages-ok"), ("codez", "responses-ok")):
            result = self.run_model(self.cfg(api_protocol=name))
            self.assertEqual(result["text"], expected)
            self.assertEqual(result["usage"]["output_tokens"], 7)
            body = CALLS[-1]
            literal = body.get("instructions", body.get("system", (body.get("messages") or [{}])[0].get("content")))
            self.assertEqual(literal, "  literal\n规则正文\n")

    def test_empty_user_box_is_preserved_and_evidence_stays_separate(self):
        self.run_model(self.cfg(video_user_prompt=""))
        self.assertEqual(CALLS[-1]["messages"][1]["content"], [{"type": "text", "text": ""}, {"type": "text", "text": "separate evidence"}])

    def test_parallel_runs_do_not_share_history(self):
        def task(name):
            return self.run_model(self.cfg(video_user_prompt=name))["text"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(task, ["workflow-A", "workflow-B"]))
        self.assertEqual(values, ["echo:workflow-A", "echo:workflow-B"])
        self.assertTrue(all(len(body["messages"]) == 2 for body in CALLS))

    def test_tool_replay_preserves_reasoning_and_cleans_worker(self):
        with patch.object(backend, "MediaWorker", FakeWorker):
            result = self.run_model(self.cfg(model="tool", media_mcp={"args": []}))
        self.assertEqual(FakeWorker.called, 1)
        self.assertEqual(FakeWorker.closed, 1)
        assistant = next(m for m in CALLS[-1]["messages"] if m["role"] == "assistant")
        self.assertEqual(assistant["reasoning_content"], "retained reasoning")
        self.assertEqual(len([e for e in result["events"] if e["type"] == "tool/call"]), 1)

    def test_duplicate_call_id_does_not_repeat_tool_side_effect(self):
        with patch.object(backend, "MediaWorker", FakeWorker):
            result = self.run_model(self.cfg(model="duplicate", media_mcp={"args": []}))
        self.assertEqual(FakeWorker.called, 1)
        self.assertTrue(result["text"].startswith("echo:"))

    def test_messages_and_responses_tool_continuations_preserve_reasoning(self):
        for protocol_name, expected in (("deepseek-messages", "messages-ok"), ("codez", "responses-ok")):
            with patch.object(backend, "MediaWorker", FakeWorker):
                result = self.run_model(self.cfg(model="tool", api_protocol=protocol_name, media_mcp={"args": []}))
            self.assertEqual(result["text"], expected)
            body = CALLS[-1]
            if protocol_name == "codez":
                self.assertEqual(next(item for item in body["input"] if item.get("type") == "reasoning")["encrypted_content"], "opaque")
            else:
                assistant = next(m for m in body["messages"] if m["role"] == "assistant")
                self.assertEqual(assistant["content"][0]["signature"], "signed")
        self.assertEqual(FakeWorker.called, 2)
        self.assertEqual(FakeWorker.closed, 2)

    def test_invalid_arguments_are_reported_without_executing(self):
        with patch.object(backend, "MediaWorker", FakeWorker):
            self.run_model(self.cfg(model="invalid-tool", media_mcp={"args": []}))
        self.assertEqual(FakeWorker.called, 0)
        self.assertIn("Invalid tool arguments", next(m for m in CALLS[-1]["messages"] if m["role"] == "tool")["content"])

    def test_unavailable_tool_rejected_and_worker_closed(self):
        with patch.object(backend, "MediaWorker", FakeWorker):
            with self.assertRaises(backend.BridgeError):
                self.run_model(self.cfg(model="unavailable", media_mcp={"args": []}))
        self.assertEqual(FakeWorker.closed, 1)
        self.assertEqual(FakeWorker.called, 0)

    def test_empty_or_truncated_final_output_fails(self):
        for model in ("empty", "truncated-output"):
            with self.assertRaises(backend.BridgeError):
                self.run_model(self.cfg(model=model))

    def test_stream_empty_retries_three_times_with_identical_rules_and_images(self):
        image = {"type":"image", "mimeType":"image/png",
                 "data":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jOioAAAAASUVORK5CYII="}
        result = self.run_model(self.cfg(model="empty-stream-three", _empty_response_retries=3,
                                        max_agent_rounds=1), blocks=[image], timeout=15)
        self.assertEqual(result["text"], "stream-recovered")
        self.assertEqual(len(CALLS), 4)
        self.assertTrue(all(body == CALLS[0] for body in CALLS))
        self.assertTrue(any(b["type"] == "image_url" for b in CALLS[0]["messages"][1]["content"]))
        self.assertEqual(result["usage"]["output_tokens"], 28)
        self.assertEqual([e["data"]["attempt"] for e in result["events"] if e["type"] == "step/retry"], [1,2,3])
        self.assertEqual([r["finish_reason"] for r in result["token_speed"]],
                         ["empty_response"]*3 + ["completed"])

    def test_empty_retry_exhaustion_stops_after_four_requests(self):
        with self.assertRaisesRegex(backend.BridgeError, "已额外重试3次仍失败"):
            self.run_model(self.cfg(model="empty", _empty_response_retries=3), timeout=15)
        self.assertEqual(len(CALLS), 4)
        self.assertTrue(all(body == CALLS[0] for body in CALLS))

    def test_empty_retry_preserves_completed_tools(self):
        with patch.object(backend, "MediaWorker", FakeWorker):
            result = self.run_model(self.cfg(model="tool-empty-once", media_mcp={"args": []},
                                            _empty_response_retries=3))
        self.assertEqual(result["text"], "echo:my task")
        self.assertEqual(len(CALLS), 3)
        self.assertEqual(CALLS[1], CALLS[2])
        self.assertEqual(FakeWorker.called, 1)
        self.assertEqual(FakeWorker.closed, 1)

    def test_other_stages_keep_empty_response_without_retries(self):
        with self.assertRaisesRegex(backend.BridgeError, "未自动重跑"):
            self.run_model(self.cfg(model="empty"))
        self.assertEqual(len(CALLS), 1)

    def test_retry_backoff_obeys_timeout(self):
        with self.assertRaisesRegex(backend.BridgeError, "timeout"):
            self.run_model(self.cfg(model="empty", _empty_response_retries=3), timeout=.2)
        self.assertEqual(len(CALLS), 1)

    def test_cancel_during_empty_retry_stops_further_requests(self):
        runtime = backend.SDKRuntime(self.cfg(model="empty", _empty_response_retries=3))
        def progress(event):
            if event == "step/retry":
                runtime.cancel()
        try:
            with self.assertRaisesRegex(backend.BridgeError, "cancelled"):
                runtime.run("cancel-retry", [], 5, progress)
        finally:
            runtime.stop()
        self.assertEqual(len(CALLS), 1)

    def test_output_limit_does_not_trigger_empty_retry(self):
        with self.assertRaisesRegex(backend.BridgeError, "provider_finish"):
            self.run_model(self.cfg(model="truncated-output", _empty_response_retries=3))
        self.assertEqual(len(CALLS), 1)

    def test_cancel_closes_a_stalled_stream_without_retry(self):
        runtime = backend.SDKRuntime(self.cfg(model="blocked"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(runtime.run, "cancel-test", [], 30)
            self.assertTrue(STARTED.wait(3))
            began = time.monotonic()
            runtime.cancel()
            runtime.cancel()  # Repeated cancellation must not interrupt cleanup.
            with self.assertRaises(backend.BridgeError):
                future.result(timeout=2)
            self.assertLess(time.monotonic() - began, 2)
        runtime.stop()
        self.assertEqual(len(CALLS), 1)
        self.assertFalse(runtime.alive)
        self.assertFalse(runtime.active_session)

    def test_timeout_closes_worker(self):
        runtime = backend.SDKRuntime(self.cfg(model="blocked", media_mcp={"args": []}))
        with patch.object(backend, "MediaWorker", FakeWorker), self.assertRaises(backend.BridgeError):
            runtime.run("timeout-test", [], .15)
        runtime.stop()
        self.assertEqual(FakeWorker.closed, 1)

    def test_chat_stream_fragments_and_reasoning(self):
        stream = Stream("chat")
        for delta, finish in [({"reasoning_content": "thought", "tool_calls": [{"index": 0, "id": "id1", "function": {"name": "echo", "arguments": '{"value":'}}]}, None),
                              ({"tool_calls": [{"index": 0, "function": {"arguments": '"ok"}'}}]}, "tool_calls")]:
            stream.consume(json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}))
        stream.consume("[DONE]")
        raw = stream.result()["choices"][0]["message"]
        self.assertEqual(raw["reasoning_content"], "thought")
        self.assertEqual(json.loads(raw["tool_calls"][0]["function"]["arguments"]), {"value": "ok"})

    def test_messages_stream_keeps_thinking_signature(self):
        stream = Stream("messages")
        events = [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "keep"}},
                  {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "signed"}},
                  {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}, {"type": "message_stop"}]
        for event in events:
            stream.consume(json.dumps(event))
        self.assertEqual(stream.result()["content"][0], {"type": "thinking", "thinking": "keep", "signature": "signed"})

    def test_responses_stream_preserves_encrypted_reasoning_and_calls(self):
        stream = Stream("responses")
        item = {"type": "reasoning", "encrypted_content": "opaque", "summary": []}
        stream.consume(json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item}))
        stream.consume(json.dumps({"type": "response.completed", "response": {"status": "completed", "output": []}}))
        self.assertEqual(stream.result()["output"], [item])

    def test_incomplete_stream_is_not_accepted(self):
        stream = Stream("chat")
        stream.consume(json.dumps({"choices": [{"delta": {"content": "partial"}}]}))
        with self.assertRaises(backend.BridgeError):
            stream.result()

    def test_endpoint_rejects_credentials_and_query(self):
        for url in ("http://key:secret@example.com/v1", "https://example.com/v1?key=secret", "file:///tmp/model"):
            with self.assertRaises(backend.BridgeError):
                endpoint(self.cfg(base_url=url))

    def test_transport_categories_never_expose_raw_exception_or_credentials(self):
        key = ConnectionKey("example.com", 443, True, True, None, None, None)
        secret = "secret-in-exception-and-api-key"
        cases = [
            (backend.aiohttp.ClientConnectorError(key, socket.gaierror(-2, secret)), "DNS解析失败"),
            (backend.aiohttp.ClientConnectorError(key, OSError(101, secret)), "无法建立连接"),
            (backend.aiohttp.ClientConnectorCertificateError(key, ssl.CertificateError(secret)), "TLS证书校验失败"),
            (backend.aiohttp.ClientProxyConnectionError(key, OSError(111, secret)), "代理连接失败"),
            (backend.aiohttp.ServerDisconnectedError(secret), "对端关闭连接"),
            (backend.aiohttp.ClientPayloadError(secret), "响应内容传输不完整"),
        ]
        for error, label in cases:
            with self.subTest(error=type(error).__name__):
                result = transport_error(error, self.cfg(api_key=secret, base_url="https://example.com/private-"+secret),
                    response_started=False, elapsed=1.25, request_bytes=1048576)
                self.assertEqual(result.code, "provider_transport")
                self.assertIn(label, str(result))
                self.assertIn("主机=example.com", str(result))
                self.assertIn("1.00 MiB", str(result))
                self.assertNotIn(secret, str(result))
                self.assertIsNone(result.__cause__)

    def test_interrupted_http_body_reports_transport_stage_and_does_not_retry(self):
        with self.assertRaisesRegex(backend.BridgeError, "响应内容传输不完整") as caught:
            self.run_model(self.cfg(model="broken-response"))
        self.assertIn("阶段=接收响应", str(caught.exception))
        self.assertIn("ClientPayloadError", str(caught.exception))
        self.assertEqual(len(CALLS), 1)
        self.assertEqual(backend.runtime_status()["active_turns"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
