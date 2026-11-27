"""Real scheduler and native savers: both modes release VIDEO before frames/AI."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-async-save-test-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "data")
sys.path.insert(0, os.environ.get("COMFYUI_PATH", "/home/comfy/ComfyUI"))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management as model_management
import nodes as comfy_nodes
import folder_paths
import torch
from comfy_api.latest import io, InputImpl
from comfy_execution.graph_utils import ExecutionBlocker
from execution import PromptExecutor, CacheType
from comfy_extras.nodes_video import SaveVideo

package = types.ModuleType("kk_async_save_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")
chain_b = importlib.import_module(package.__name__ + ".kkagent_bridge.chain_b")
preparation = importlib.import_module(package.__name__ + ".kkagent_bridge.b_video_preparation")
parallel = importlib.import_module(package.__name__ + ".kkagent_bridge.b_parallel_reverse")
graph_migration = importlib.import_module(package.__name__ + ".kkagent_bridge.stage_graph")


class AsyncSaveTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = Path(TEMP.name) / "source.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=64x96:rate=10:duration=1",
            "-c:v", "libx264", "-threads", "1", str(cls.source)], check=True)

    async def check_overlap(self, mode, duration, chain, outputs):
        model_management.interrupt_current_processing(False)
        events = {name: threading.Event() for name in ("selecting", "video-saved", "image-saved", "closed")}
        calls = []
        image = torch.ones((1, 12, 24, 3))
        manifest = json.dumps({"frames": [{"segment_index": 1}]})
        video = InputImpl.VideoFromFile(str(self.source))
        videos = (video, *[ExecutionBlocker(None) for _ in range(4)])
        prepared = ("", "{}", "video-ready", *videos, [], '{"frames":[]}')
        frame_result = (*prepared[:8], [image], manifest)

        def generator(*unused, **kwargs):
            calls.append(chain)
            try:
                yield prepared
                events["selecting"].set()
                if not events["video-saved"].wait(3):
                    raise AssertionError("VIDEO was held until selection/reverse completed")
                if chain == "B":
                    yield frame_result
                    if not events["image-saved"].wait(3):
                        raise AssertionError("IMAGE was held until final prompt review completed")
                return ("finished", *frame_result[1:])
            finally:
                events["closed"].set()

        class Source(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="KKAsyncSource", inputs=[], outputs=[io.Video.Output()])

            @classmethod
            def execute(cls):
                return io.NodeOutput(types.SimpleNamespace(get_duration=lambda: duration))

        class Entry(stages.KKGO_VideoSegments):
            _RETURN_TYPES = _RETURN_NAMES = _OUTPUT_IS_LIST = _OUTPUT_TOOLTIPS = None

            @classmethod
            def define_schema(cls):
                schema = super().define_schema()
                schema.node_id = "KKAsyncEntry"
                return schema

            iter_chain_a = classmethod(generator)

        class ProbeVideo(SaveVideo):
            @classmethod
            def define_schema(cls):
                schema = super().define_schema()
                schema.node_id = "KKAsyncSaveVideo"
                return schema

            @classmethod
            def execute(cls, **kwargs):
                # Deliberately synchronous, like the real standard saving node.
                if not events["selecting"].wait(3):
                    raise AssertionError("background AI was not submitted before saving")
                result = super().execute(**kwargs)
                events["video-saved"].set()
                return result

        class ProbeImage(comfy_nodes.SaveImage):
            def save_images(self, *args, **kwargs):
                result = super().save_images(*args, **kwargs)
                events["image-saved"].set()
                return result

        directory = Path(TEMP.name) / f"{duration}-{mode}-{outputs[0]}"
        directory.mkdir(exist_ok=True)
        prompt = {
            "source": {"class_type": "KKAsyncSource", "inputs": {}},
            "entry": {"class_type": "KKAsyncEntry", "inputs": {"video": ["source", 0],
                "api_key": "offline", "review_api_key": "offline", "duration_mode": mode,
                "rules_text": "A", "chain_a_review_rules_text": "A review", "segment_rules_text": "B segment",
                "chain_b_reverse_rules_text": "B reverse", "chain_b_review_rules_text": "B review"}},
            "save": {"class_type": "KKAsyncSaveVideo", "inputs": {"video": ["entry", 3],
                "filename_prefix": "early", "format": "mp4", "codec": {"codec": "h264"}}},
            "reverse": {"class_type": "KKGO_VideoReverse", "inputs": {"context": ["entry", 8]}},
        }
        mappings = {"KKAsyncSource": Source, "KKAsyncEntry": Entry, "KKAsyncSaveVideo": ProbeVideo,
            "KKGO_VideoReverse": stages.KKGO_VideoReverse, "KKGO_VideoFrames": stages.KKGO_VideoFrames,
            "KKAsyncSaveImage": ProbeImage}
        if chain == "B":
            prompt["frames"] = {"class_type": "KKGO_VideoFrames", "inputs": {"context": ["entry", 8]}}
            prompt["image"] = {"class_type": "KKAsyncSaveImage", "inputs": {
                "images": ["frames", 2], "filename_prefix": "frame"}}
            outputs = [*outputs, "image"]
        server = types.SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda *args: None)
        executor = PromptExecutor(server, cache_type=CacheType.CLASSIC, cache_args={"ram": 0, "ram_inactive": 0})
        with patch.dict(comfy_nodes.NODE_CLASS_MAPPINGS, mappings), \
                patch.object(stages, "iter_chain_b", side_effect=generator), \
                patch.object(folder_paths, "output_directory", str(directory)):
            await executor.execute_async(prompt, f"overlap-{duration}-{mode}", execute_outputs=outputs)
        self.assertTrue(executor.success)
        self.assertEqual(calls, [chain], "the two consumers must share one AI job")
        self.assertTrue(events["closed"].is_set())
        self.assertEqual(len(list(directory.glob("early_*.mp4"))), 1)
        if chain == "B":
            self.assertEqual(len(list(directory.glob("frame_*.png"))), 1)
        cached = await executor.caches.outputs.get("reverse")
        self.assertEqual(cached.outputs[0], ["finished"])

    async def test_both_modes_and_both_chains_overlap_native_video_saving(self):
        for mode, threshold in (("32秒模式（原模式）", 32), ("17秒模式", 17)):
            for duration, chain in ((threshold - 1, "A"), (threshold, "B")):
                for outputs in (["save", "reverse"], ["reverse", "save"]):
                    with self.subTest(mode=mode, chain=chain, outputs=outputs):
                        await self.check_overlap(mode, duration, chain, outputs)

    def test_api_migration_preserves_savers_and_shares_one_frame_node(self):
        prompt = {
            "entry": {"class_type": "KKGO_VideoSegments", "inputs": {}},
            "reverse": {"class_type": "KKGO_VideoReverse", "inputs": {"context": ["entry", 8]}},
            "video": {"class_type": "SaveVideo", "inputs": {"video": ["entry", 3], "filename_prefix": "chosen"}},
            "image": {"class_type": "SaveImage", "inputs": {"images": ["entry", 11], "filename_prefix": "chosen-image"}},
            "image2": {"class_type": "SaveImage", "inputs": {"images": ["reverse", 9]}},
            "info": {"class_type": "SaveText", "inputs": {"text": ["entry", 10]}},
            "unrelated": {"class_type": "Other", "inputs": {"value": [{}, 1]}},
        }
        payload = {"prompt": prompt}
        graph_migration.migrate_frame_prompt(payload)
        frame_id = next(k for k, v in prompt.items() if v["class_type"] == "KKGO_VideoFrames")
        self.assertEqual(prompt["image"]["inputs"]["images"], [frame_id, 2])
        self.assertEqual(prompt["image2"]["inputs"]["images"], [frame_id, 6])
        self.assertEqual(prompt["info"]["inputs"]["text"], [frame_id, 1])
        self.assertEqual(prompt["video"]["inputs"], {"video": ["entry", 3], "filename_prefix": "chosen"})
        before = json.dumps(payload, sort_keys=True)
        graph_migration.migrate_frame_prompt(payload)
        self.assertEqual(json.dumps(payload, sort_keys=True), before)

    async def test_real_b_generator_yields_video_before_coordinator_or_frames(self):
        for mode, limit in (("32秒模式（原模式）", 30), ("17秒模式", 15)):
            with self.subTest(mode=mode):
                directory = Path(TEMP.name) / f"real-b-{limit}"
                directory.mkdir()
                report = {"video": {"duration_seconds": 40}}
                videos = ("video1", "video2", None, None, None)
                record = {"count": 2, "segments": [{"start": 0, "end": 20}, {"start": 20, "end": 40}]}
                bridge = types.SimpleNamespace(cfg={"media_model_dir": "/unused", "media_mcp": {}},
                    session_id=lambda name: name, ask=lambda *a, **kw: {"text": "done"}, close=lambda: None)
                from contextlib import nullcontext
                node = types.SimpleNamespace(hidden=types.SimpleNamespace(unique_id="offline-entry"))
                with patch.object(chain_b, "workflow_bridge", return_value=nullcontext(bridge)), \
                        patch.object(chain_b, "resolve_review_ai"), \
                        patch.object(chain_b, "configure_stage"), \
                        patch.object(chain_b, "evidence", return_value={}), \
                        patch.object(chain_b, "MediaTools") as tools, \
                        patch.object(preparation, "prepare_b_chain_media", return_value=(directory, report, [], [], [], "evidence")), \
                        patch.object(chain_b, "read_tool_videos", return_value=(videos, record)) as read, \
                        patch.object(parallel, "iter_parallel_reverse", side_effect=AssertionError("AI started before VIDEO yield")) as reverse:
                    job = chain_b.iter_chain_b(node, None, {"reference_limit_seconds": limit}, "offline",
                        "http://127.0.0.1:1", "offline", "user", "segment", "reverse", 900, 100,
                        "", "openai-completions", "high", True, True, None, (), "review", {})
                    try:
                        result = next(job)
                        self.assertEqual(result[3:8], videos)
                        read.assert_called_once()
                        self.assertEqual(read.call_args.kwargs["reference_limit_seconds"], limit)
                        reverse.assert_not_called()
                    finally:
                        job.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
