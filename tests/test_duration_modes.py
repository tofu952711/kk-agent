"""Duration modes select matching lazy rules and execution paths; no model calls."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("COMFYUI_PATH", str(ROOT.parents[1])))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management
import nodes as comfy_nodes
from comfy_api.latest import io
from comfy_api.latest import InputImpl
from comfy_execution.graph_utils import ExecutionBlocker
from execution import PromptExecutor, CacheType

package = types.ModuleType("kk_duration_mode_tests")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
agent = importlib.import_module(package.__name__ + ".nodes.kkgo_video_agent")
stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")
routing = importlib.import_module(package.__name__ + ".kkagent_bridge.duration_routing")
OLD = routing.DEFAULT_DURATION_MODE
NEW = "17秒模式"
ENTRY_CLASSES = (agent.KKGO_FullVideoAgent, stages.KKGO_VideoSegments)


class Video:
    def __init__(self, duration):
        self.duration = duration

    def get_duration(self):
        return self.duration


def prepared(chain):
    return (chain, "{}", "prepared", *[ExecutionBlocker(None) for _ in range(5)], [], '{"frames":[]}')


class DurationModeTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_boundaries_default_and_limit(self):
        for mode, threshold in ((OLD, 32), (NEW, 17)):
            for duration in (1., 16.999999, 17., 20., 31.999999, 32., 60.):
                with self.subTest(mode=mode, duration=duration):
                    route = routing.route_duration(duration, mode)
                    self.assertEqual(route["chain"], "A" if duration < threshold else "B")
                    self.assertEqual(route["duration_mode"], mode)
                    self.assertEqual(route["split_threshold_seconds"], threshold)
                    self.assertIn(f"不足{threshold}秒", route["boundary_note"])
                    if mode == OLD:
                        self.assertEqual(routing.route_duration(duration), route)
            for duration in (0, -1, float("nan"), float("inf"), 60.001):
                with self.subTest(mode=mode, invalid=duration), self.assertRaises(ValueError):
                    routing.route_duration(duration, mode)

    def test_only_active_chain_rules_are_requested(self):
        for cls in ENTRY_CLASSES:
            for mode, threshold in ((OLD, 32), (NEW, 17)):
                for duration in (16.999999, 17., 20., 31.999999, 32.):
                    expected = (["rules_text", "chain_a_review_rules_text"] if duration < threshold else
                                ["segment_rules_text", "chain_b_reverse_rules_text", "chain_b_review_rules_text"])
                    with self.subTest(node=cls.__name__, mode=mode, duration=duration):
                        self.assertEqual(cls.check_lazy_status(Video(duration), duration_mode=mode,
                            rules_text=None, chain_a_review_rules_text=None, segment_rules_text=None,
                            chain_b_reverse_rules_text=None, chain_b_review_rules_text=None), expected)

    def test_schema_appends_optional_default_without_changing_ports(self):
        for cls, count in zip(ENTRY_CLASSES, (15, 9)):
            schema = cls.define_schema()
            self.assertEqual(len(schema.outputs), count)
            mode = next(item for item in schema.inputs if item.id == "duration_mode")
            self.assertTrue(mode.optional)
            self.assertEqual(mode.default, OLD)
            self.assertEqual(list(mode.options), [OLD, NEW])
            self.assertIn("base_url", [item.id for item in schema.inputs])
            widgets = [item.id for item in schema.inputs if item.id in {"api_key", "base_url", "requirement"}]
            self.assertEqual(widgets, ["api_key", "base_url", "requirement"])

    def test_exact_17_second_b_video_can_be_cut_and_saved(self):
        tools_module = importlib.import_module(package.__name__ + ".kkagent_bridge.media_tools")
        ffmpeg_module = importlib.import_module(package.__name__ + ".kkagent_bridge.ffmpeg_segments")
        chain_b = importlib.import_module(package.__name__ + ".kkagent_bridge.chain_b")
        with tempfile.TemporaryDirectory(prefix="kk-mode-cut-") as temporary:
            root = Path(temporary)
            source, output = root / "source-video.mp4", root / "media-analysis"
            ffmpeg, _ = ffmpeg_module.executables()
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "testsrc2=size=64x96:rate=10:duration=17", "-c:v", "libx264", "-threads", "1", str(source)],
                check=True, capture_output=True)
            output.mkdir()
            tools = tools_module.MediaTools(source, output)
            info = tools.info()
            route = routing.route_duration(info["duration_seconds"], NEW)
            self.assertEqual(route["chain"], "B")
            (output / "duration_policy.json").write_text(json.dumps({**route, "phase":"segment"}))
            result = tools.set_video_segments([{"start":0., "end":8.}, {"start":8., "end":17.}])
            self.assertEqual(result["status"], "ready")
            videos, record = chain_b.read_tool_videos(output, info)
            self.assertEqual(record["count"], 2)
            for index, (video, duration) in enumerate(zip(videos, (8., 9.))):
                target = root / f"saved-{index}.mp4"
                video.save_to(str(target), crf=None)
                self.assertAlmostEqual(InputImpl.VideoFromFile(str(target)).get_duration(), duration, delta=.11)

    async def test_both_entry_points_dispatch_using_selected_mode(self):
        for cls in ENTRY_CLASSES:
            for mode, threshold in ((OLD, 32), (NEW, 17), (None, 32)):
                for duration in (16.999999, 17., 20., 32., 60.):
                    expected = "A" if duration < threshold else "B"
                    seen = []

                    def capture(chain):
                        def run(*args, **kwargs):
                            record = next(value for value in args if isinstance(value, dict) and "chain" in value)
                            seen.append(record)
                            return prepared(chain)
                        return run

                    def generate(chain):
                        def run(*args, **kwargs):
                            result = capture(chain)(*args, **kwargs)
                            yield result
                            return result
                        return run

                    kwargs = {} if mode is None else {"duration_mode": mode}
                    with self.subTest(node=cls.__name__, mode=mode, duration=duration), \
                         patch.object(cls, "run_chain_a", side_effect=capture("A")), \
                         patch.object(cls, "run_chain_b", side_effect=capture("B")), \
                         patch.object(cls, "iter_chain_a", side_effect=generate("A")), \
                         patch.object(stages, "iter_chain_b", side_effect=generate("B")):
                        result = await cls.execute(Video(duration), api_key="offline", review_api_key="offline", **kwargs)
                        self.assertEqual(result[0], expected)
                        self.assertEqual(seen[0]["chain"], expected)
                        self.assertEqual(seen[0]["split_threshold_seconds"], threshold)
                        if cls is stages.KKGO_VideoSegments:
                            result[8].close()

    async def test_real_executor_loads_only_selected_lazy_rules(self):
        for base in ENTRY_CLASSES:
            for mode, expected in ((OLD, "A"), (NEW, "B")):
                called, received = [], []

                class Source(io.ComfyNode):
                    @classmethod
                    def define_schema(cls):
                        return io.Schema(node_id="KKModeSource", inputs=[], outputs=[io.Video.Output()])

                    @classmethod
                    def execute(cls):
                        return io.NodeOutput(Video(20))

                class Rule(io.ComfyNode):
                    @classmethod
                    def define_schema(cls):
                        return io.Schema(node_id="KKModeRule", inputs=[io.String.Input("name")], outputs=[io.String.Output()])

                    @classmethod
                    def execute(cls, name):
                        called.append(name)
                        return io.NodeOutput(name)

                class Entry(base):
                    _RETURN_TYPES = _RETURN_NAMES = _OUTPUT_IS_LIST = _OUTPUT_TOOLTIPS = None

                    @classmethod
                    def define_schema(cls):
                        schema = super().define_schema()
                        schema.node_id = "KKModeEntry"
                        return schema

                    @classmethod
                    def run_chain_a(cls, *args, **kwargs):
                        return prepared("A")

                    @classmethod
                    def run_chain_b(cls, *args, **kwargs):
                        return prepared("B")

                    @classmethod
                    def iter_chain_a(cls, *args, **kwargs):
                        yield prepared("A")
                        return prepared("A")

                def iter_b(*args, **kwargs):
                    yield prepared("B")
                    return prepared("B")

                class Sink(io.ComfyNode):
                    @classmethod
                    def define_schema(cls):
                        return io.Schema(node_id="KKModeSink", inputs=[io.String.Input("text")], outputs=[], is_output_node=True)

                    @classmethod
                    def execute(cls, text):
                        received.append(text)
                        return io.NodeOutput()

                prompt = {
                    "source": {"class_type": "KKModeSource", "inputs": {}},
                    "entry": {"class_type": "KKModeEntry", "inputs": {"video": ["source", 0],
                        "api_key": "offline", "review_api_key": "offline", "duration_mode": mode}},
                    "sink": {"class_type": "KKModeSink", "inputs": {"text": ["entry", 0]}},
                }
                for name in ("rules_text", "chain_a_review_rules_text", "segment_rules_text",
                             "chain_b_reverse_rules_text", "chain_b_review_rules_text"):
                    prompt[name] = {"class_type": "KKModeRule", "inputs": {"name": name}}
                    prompt["entry"]["inputs"][name] = [name, 0]
                server = types.SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda *args: None)
                executor = PromptExecutor(server, cache_type=CacheType.CLASSIC, cache_args={"ram": 0, "ram_inactive": 0})
                with patch.dict(comfy_nodes.NODE_CLASS_MAPPINGS, {"KKModeSource": Source, "KKModeRule": Rule,
                        "KKModeEntry": Entry, "KKModeSink": Sink}), patch.object(stages, "iter_chain_b", side_effect=iter_b):
                    await executor.execute_async(prompt, f"mode-{base.__name__}-{expected}", execute_outputs=["sink"])
                self.assertTrue(executor.success)
                self.assertEqual(received, [expected])
                selected = ({"rules_text", "chain_a_review_rules_text"} if expected == "A" else
                            {"segment_rules_text", "chain_b_reverse_rules_text", "chain_b_review_rules_text"})
                self.assertEqual(set(called), selected)
                if base is stages.KKGO_VideoSegments:
                    cached = await executor.caches.outputs.get("entry")
                    cached.outputs[8][0].close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
