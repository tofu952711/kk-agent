"""Real Comfy executor, cancel routes and edited VIDEO regressions; no paid APIs."""
import asyncio
import importlib
import io as buffer_io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import av
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

PLUGIN = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-node-lifecycle-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "records")
sys.path.insert(0, os.environ.get("COMFYUI_PATH", str(PLUGIN.parents[1])))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management as model_management
import nodes as comfy_nodes
from comfy_api.latest import InputImpl, io
from comfy_execution.graph_utils import ExecutionBlocker
from comfy_execution.utils import CurrentNodeContext
from execution import CacheType, PromptExecutor

package = types.ModuleType("kk_lifecycle_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")
cancellation = importlib.import_module(package.__name__ + ".kkagent_bridge.cancellation")
media = importlib.import_module(package.__name__ + ".kkagent_bridge.media_workflow")
workflow = importlib.import_module(package.__name__ + ".kkagent_bridge.workflow")
monitoring = importlib.import_module(package.__name__ + ".kkagent_bridge.token_speed")


class NodeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(TEMP.name) / "source.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=10:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=3",
                        "-c:v", "libx264", "-threads", "1", "-c:a", "aac", "-shortest", str(cls.source)], check=True)

    def setUp(self):
        model_management.interrupt_current_processing(False)

    async def test_cancel_aliases_latch_even_after_sibling_consumes_flag(self):
        app = web.Application()
        cancellation.install_interrupt_hook(types.SimpleNamespace(app=app))

        async def handler(request):
            if request.query.get("reject"):
                return web.Response(status=403)
            model_management.interrupt_current_processing(True)
            try:
                model_management.throw_exception_if_processing_interrupted()
            except model_management.InterruptProcessingException:
                pass
            return web.Response(status=200)

        app.router.add_route("*", "/{path:.*}", handler)
        async with TestClient(TestServer(app)) as client:
            cases = [("/interrupt", {}), ("/api/interrupt", {}),
                     ("/interrupt", {"prompt_id": "running"}),
                     ("/api/interrupt", {"prompt_id": "running"}),
                     ("/api/jobs/running/cancel", None)]
            for path, body in cases:
                with self.subTest(path=path, body=body):
                    with CurrentNodeContext("running", "entry"):
                        selected = cancellation.CancelScope()
                    with CurrentNodeContext("other", "entry"):
                        other = cancellation.CancelScope()
                    try:
                        async with client.post(path, json=body) as response:
                            self.assertEqual(response.status, 200)
                        self.assertFalse(model_management.processing_interrupted())
                        self.assertTrue(selected.cancelled.is_set())
                        targeted = bool(body) or "/jobs/" in path
                        self.assertEqual(other.cancelled.is_set(), not targeted)
                    finally:
                        selected.close()
                        other.close()

            for path in ["/api/interrupt?reject=1", "/api/jobs/running/cancel?reject=1"]:
                with CurrentNodeContext("running", "entry"):
                    selected = cancellation.CancelScope()
                try:
                    async with client.post(path, json={}) as response:
                        self.assertEqual(response.status, 403)
                    self.assertFalse(selected.cancelled.is_set())
                finally:
                    selected.close()

    async def test_staging_preserves_crop_trim_and_audio(self):
        original = InputImpl.VideoFromFile(str(self.source))
        edited = original.as_cropped(32, 32, 64, 64).as_trimmed(1, 1, strict_duration=True)
        destination = Path(TEMP.name) / "edited.mp4"
        await asyncio.to_thread(media.stage_video, edited, destination)
        staged = InputImpl.VideoFromFile(str(destination))
        self.assertEqual(staged.get_dimensions(), edited.get_dimensions())
        self.assertEqual(staged.get_dimensions(), (64, 64))
        self.assertAlmostEqual(float(staged.get_duration()), 1, delta=.2)
        with av.open(str(destination)) as container:
            self.assertEqual(len(container.streams.audio), 1)

    async def test_plain_video_staging_keeps_compressed_video_packets(self):
        destination = Path(TEMP.name) / "plain.mp4"
        await asyncio.to_thread(media.stage_video, InputImpl.VideoFromFile(str(self.source)), destination)

        def packets(path):
            with av.open(str(path)) as container:
                return [bytes(packet) for packet in container.demux(video=0) if packet.size]

        self.assertEqual(packets(self.source), packets(destination))

    async def run_prompt(self, prompt_id, *, fail=False, reverse=False, active_reverse=False):
        resources = {"reverse_started": threading.Event()}
        source = self.source

        class Source(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="KKTestSource", inputs=[], outputs=[io.Video.Output()])

            @classmethod
            def execute(cls):
                return io.NodeOutput(InputImpl.VideoFromFile(str(source)))

        class Entry(stages.KKGO_VideoSegments):
            @classmethod
            def define_schema(cls):
                schema = super().define_schema()
                schema.node_id = "KKTestEntry"
                return schema

            @classmethod
            def iter_chain_a(cls, video, *unused, **kwargs):
                with workflow.workflow_bridge("offline-placeholder", "http://127.0.0.1:1/v1",
                                               "offline", 30) as bridge, monitoring.SpeedRun(cls.hidden.unique_id, "offline"):
                    root = Path(bridge.cfg["workspace"])
                    root.mkdir(parents=True)
                    (root / "marker").write_text("prepared")
                    resources["workspace"] = root
                    resources["scope"] = cancellation.current_scope()
                    resources["watcher"] = next(t for t in threading.enumerate()
                                                if t.name == "video-cancel" and t.ident not in resources["initial_threads"])
                    independent_video = InputImpl.VideoFromFile(buffer_io.BytesIO(source.read_bytes()))
                    videos = (independent_video, *[ExecutionBlocker(None) for _ in range(4)])
                    yield ("", "{}", "prepared", *videos)
                    resources["reverse_calls"] = resources.get("reverse_calls", 0) + 1
                    if active_reverse:
                        resources["reverse_started"].set()
                        while True:
                            cancellation.check_cancelled()
                            time.sleep(.01)
                    return ("done", "{}", "ok", *videos)

        class Sink(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="KKTestSink", is_output_node=True,
                                 inputs=[io.Video.Input("video")], outputs=[])

            @classmethod
            async def execute(cls, video):
                resources["saved_video"] = video
                if fail:
                    if active_reverse:
                        if not await asyncio.to_thread(resources["reverse_started"].wait, 2):
                            raise AssertionError("reverse worker did not start")
                    raise RuntimeError("intentional downstream failure")
                return io.NodeOutput()

        prompt = {
            "source": {"class_type": "KKTestSource", "inputs": {}},
            "entry": {"class_type": "KKTestEntry", "inputs": {
                "video": ["source", 0], "api_key": "offline-placeholder", "base_url": "http://127.0.0.1:1/v1",
                "model": "offline", "requirement": "test", "rules_text": "test"}},
            "save": {"class_type": "KKTestSink", "inputs": {"video": ["entry", 3]}},
        }
        outputs = ["save"]
        mappings = {"KKTestSource": Source, "KKTestEntry": Entry, "KKTestSink": Sink,
                    "KKGO_VideoReverse": stages.KKGO_VideoReverse}
        if reverse:
            prompt["reverse"] = {"class_type": "KKGO_VideoReverse", "inputs": {"context": ["entry", 8]}}
            outputs.append("reverse")
        resources["initial_threads"] = {t.ident for t in threading.enumerate()}
        server = types.SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda *args: None)
        executor = PromptExecutor(server, cache_type=CacheType.CLASSIC, cache_args={"ram": 0, "ram_inactive": 0})
        with patch.dict(comfy_nodes.NODE_CLASS_MAPPINGS, mappings):
            if fail:
                with self.assertLogs(level="ERROR"):
                    await executor.execute_async(prompt, prompt_id, execute_outputs=outputs)
            else:
                await executor.execute_async(prompt, prompt_id, execute_outputs=outputs)
        self.assertEqual(executor.success, not fail)
        # Keep the real Comfy cache entry alive throughout the cleanup assertions.
        cached = await executor.caches.outputs.get("entry")
        job = cached.outputs[8][0]
        for _ in range(200):
            if job._stages is None and not resources["watcher"].is_alive():
                break
            await asyncio.sleep(.02)
        self.assertIsNone(job._stages)
        self.assertFalse(resources["workspace"].exists())
        self.assertFalse(resources["watcher"].is_alive())
        self.assertNotIn(prompt_id, stages.JOB_CLEANUP.jobs)
        self.assertEqual(resources.get("reverse_calls", 0), int(reverse))
        self.assertEqual(monitoring.latest_snapshot("entry")["status"], "completed" if reverse and not fail else "cancelled")
        self.assertEqual(resources["saved_video"].get_dimensions(), (128, 128))
        return job

    async def test_successful_save_only_prompt_releases_cached_unconsumed_job(self):
        await self.run_prompt("save-only")

    async def test_failed_downstream_releases_cached_unconsumed_job(self):
        await self.run_prompt("save-failed", fail=True)

    async def test_normal_reverse_finishes_once_and_keeps_result(self):
        job = await self.run_prompt("reverse-completed", reverse=True)
        self.assertEqual(job.finish()[0], "done")

    async def test_failing_sibling_stops_active_reverse_without_blocking_executor(self):
        await self.run_prompt("reverse-interrupted", fail=True, reverse=True, active_reverse=True)

    async def test_prompt_cleanup_does_not_close_another_prompt(self):
        def generator(closed):
            try:
                yield "prepared"
            finally:
                closed.set()

        closed_a, closed_b = threading.Event(), threading.Event()
        with CurrentNodeContext("cleanup-a", "entry-a"):
            job_a = stages.VideoJob(stages=generator(closed_a))
        with CurrentNodeContext("cleanup-b", "entry-b"):
            job_b = stages.VideoJob(stages=generator(closed_b))
        try:
            job_a.prepare()
            job_b.prepare()
            await stages.JOB_CLEANUP.track(job_a)
            await stages.JOB_CLEANUP.track(job_b)
            stages.JOB_CLEANUP.on_prompt_end("cleanup-a")
            self.assertTrue(await asyncio.to_thread(closed_a.wait, 2))
            self.assertFalse(closed_b.is_set())
            self.assertFalse(job_b.scope.cancelled.is_set())
        finally:
            await asyncio.to_thread(job_a.close)
            await asyncio.to_thread(job_b.close)


if __name__ == "__main__":
    unittest.main(verbosity=2)
