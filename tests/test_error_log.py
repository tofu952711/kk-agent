"""Offline evidence tests: stalled streams, cleanup, redaction and bounded storage."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kkagent_bridge import error_log
from kkagent_bridge.python_provider import next_turn


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"KKGO_DATA_DIR": self.temp.name})
        self.env.start()
        error_log.clear()

    def tearDown(self):
        error_log.clear()
        self.env.stop()
        self.temp.cleanup()

    def trace(self, run="first", request="request"):
        return error_log.RequestTrace(lambda data: error_log.update(run, data), request,
                                      {"base_url": "https://example.com/v1", "model": "test", "api_key": "test-secret-123456"})

    def test_new_task_deletes_old_error_and_stale_requests_cannot_restore_it(self):
        error_log.begin("first", "72")
        trace = self.trace()
        trace.sending(20, 1)
        error_log.finish("first", "error", RuntimeError("failed"))
        self.assertIsNotNone(error_log.latest()["error"])
        error_log.begin("second", "72")
        self.assertFalse(error_log.log_file().exists())
        trace.failed(RuntimeError("late previous task"))
        error_log.finish("first", "error", RuntimeError("late previous task"))
        self.assertFalse(error_log.log_file().exists())
        self.assertEqual(error_log.latest(), {"error": None, "running": True})

    def test_success_removes_partial_record_and_error_has_no_age_expiry(self):
        error_log.begin("first", "72")
        self.trace().sending(20, 1)
        error_log.finish("first", "completed")
        self.assertFalse(error_log.log_file().exists())
        error_log.begin("second", "72")
        error_log.finish("second", "error", RuntimeError("failed"))
        with patch("time.time", return_value=9999999999):
            self.assertIsNotNone(error_log.latest()["error"])

    def test_response_redaction_and_size_limit(self):
        error_log.begin("first", "72")
        for index in range(40):
            trace = self.trace(request=str(index))
            trace.chunk(b'test-secret-123456 sk-abcdef0123456789 data:image/png;base64,AAAA ' + b'x' * 12000)
        error_log.finish("first", "error", RuntimeError("failed"))
        raw = error_log.log_file().read_text()
        self.assertNotIn("test-secret-123456", raw)
        self.assertNotIn("sk-abcdef", raw)
        self.assertNotIn("base64,AAAA", raw)
        self.assertLess(len(raw.encode()), 256 * 1024)
        report = error_log.latest()["error"]
        self.assertEqual(len(report["requests"]), 32)
        self.assertEqual(report["omitted_requests"], 8)

    def test_workflow_lifecycle_records_failure_and_clears_at_next_start(self):
        from kkagent_bridge.token_speed import SpeedRun
        with self.assertRaisesRegex(RuntimeError, "later node failure"):
            with SpeedRun("72", "test") as speed:
                trace = error_log.RequestTrace(speed.handle, "request", {"base_url": "https://example.com", "model": "test"})
                trace.sending(20, 1)
                raise RuntimeError("later node failure")
        self.assertEqual(error_log.latest()["error"]["error"]["message"], "later node failure")
        with SpeedRun("72", "test"):
            self.assertFalse(error_log.log_file().exists())
        self.assertIsNone(error_log.latest()["error"])

    def test_restart_reports_last_checkpoint_as_interrupted(self):
        error_log.begin("first", "72")
        self.trace().sending(20, 1)
        with patch.object(error_log, "_CURRENT", None), patch.object(error_log, "_BOOT", "new-process"):
            report = error_log.latest()["error"]
        self.assertEqual(report["status"], "interrupted")
        self.assertEqual(report["requests"][0]["state"], "waiting_response_headers")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"KKGO_DATA_DIR": self.temp.name})
        self.env.start()
        error_log.clear()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_post("/chat/completions", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.release.set()
        await self.runner.cleanup()
        error_log.clear()
        self.env.stop()
        self.temp.cleanup()

    async def handler(self, request):
        model = (await request.json())["model"]
        if model == "no-headers":
            await self.release.wait()
            return web.json_response({})
        if model == "http-error":
            return web.Response(status=401)
        if model == "json-success":
            return web.json_response({"choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}]})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        events = [{"choices": [{"index": 0, "delta": {"content": "hello test-secret-123456"}, "finish_reason": None}]}]
        if model == "done-keeps-open":
            events.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        for event in events:
            await response.write(("data: " + json.dumps(event) + "\n\n").encode())
        if model == "done-keeps-open":
            await response.write(b"data: [DONE]\n\n")
        await self.release.wait()
        return response

    async def invoke(self, model):
        error_log.begin("run", "72")
        cfg = {"model": model, "base_url": f"http://127.0.0.1:{self.port}", "api_protocol": "openai-completions", "api_key": "test-secret-123456"}
        trace = error_log.RequestTrace(lambda data: error_log.update("run", data), "request", cfg)
        async with aiohttp.ClientSession() as client:
            try:
                await asyncio.wait_for(next_turn(client, cfg, "rules", [{"role": "user", "content": [{"type": "text", "text": "test"}]}], [], trace=trace), .15)
            except BaseException as exc:
                trace.failed(exc)
                error_log.finish("run", "error", exc)
                return error_log.latest()["error"]["requests"][0]
        error_log.finish("run", "error", RuntimeError("later workflow step failed"))
        return error_log.latest()["error"]["requests"][0]

    async def test_stall_before_headers_is_distinct_from_no_body(self):
        row = await self.invoke("no-headers")
        self.assertFalse(row["response_headers_received"])
        self.assertEqual(row["response_bytes"], 0)

    async def test_partial_content_survives_timeout(self):
        row = await self.invoke("partial")
        self.assertEqual(row["http_status"], 200)
        self.assertGreater(row["response_bytes"], 0)
        self.assertGreater(row["content_characters"], 0)
        self.assertIsNotNone(row["first_content_seconds"])
        self.assertIn("hello", row["content_preview"])
        self.assertNotIn("test-secret-123456", row["response_preview"])
        self.assertFalse(row["done_received"])
        self.assertFalse(row["eof_received"])

    async def test_done_before_socket_close_is_preserved_even_when_call_times_out(self):
        row = await self.invoke("done-keeps-open")
        self.assertTrue(row["done_received"])
        self.assertFalse(row["eof_received"])
        self.assertGreater(row["content_characters"], 0)
        self.assertEqual(row["exception_type"], "TimeoutError")

    async def test_http_error_status_is_recorded(self):
        row = await self.invoke("http-error")
        self.assertEqual(row["http_status"], 401)
        self.assertEqual(row["exception_type"], "BridgeError")

    async def test_json_result_records_eof_and_content(self):
        row = await self.invoke("json-success")
        self.assertEqual(row["state"], "completed")
        self.assertTrue(row["eof_received"])
        self.assertEqual(row["content_preview"], "hello")

    async def test_runtime_exception_contains_observed_response_without_frontend(self):
        from kkagent_bridge.python_backend import SDKRuntime
        runtime = SDKRuntime({"model": "partial", "base_url": f"http://127.0.0.1:{self.port}",
                              "api_protocol": "openai-completions", "api_key": "test-secret-123456",
                              "video_system_prompt": "rules"})
        with self.assertRaises(RuntimeError) as error:
            await asyncio.to_thread(runtime.run, "session", [{"type": "text", "text": "test"}], .15)
        message = str(error.exception)
        self.assertIn("HTTP=200", message)
        self.assertIn("实际状态=收到正文", message)
        self.assertIn("hello", message)
        self.assertNotIn("test-secret-123456", message)


if __name__ == "__main__":
    unittest.main()
