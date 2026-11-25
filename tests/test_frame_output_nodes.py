"""Use the real Comfy executor and standard SaveImage with the IMAGE list port."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-image-output-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "data")
sys.path.insert(0, os.environ.get("COMFYUI_PATH", "/home/comfy/ComfyUI"))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management
import torch
import nodes as comfy_nodes
import folder_paths
from comfy_api.latest import io
from execution import PromptExecutor, CacheType
from comfy_execution.graph_utils import ExecutionBlocker

package = types.ModuleType("kk_image_port_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
stages = importlib.import_module(package.__name__ + ".nodes.kkgo_video_stages")
agent = importlib.import_module(package.__name__ + ".nodes.kkgo_video_agent")


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_images_can_save_while_reverse_is_waiting(self):
        saved = threading.Event()
        manifest = json.dumps({"frames":[{"segment_index":1}]})
        image = torch.ones((1,12,24,3))

        class Video:
            def get_duration(self): return 1.

        class Source(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="KKEarlySource",inputs=[],outputs=[io.Video.Output()])

            @classmethod
            def execute(cls): return io.NodeOutput(Video())

        class Entry(stages.KKGO_VideoSegments):
            _RETURN_TYPES = _RETURN_NAMES = _OUTPUT_IS_LIST = _OUTPUT_TOOLTIPS = None

            @classmethod
            def define_schema(cls):
                schema = super().define_schema()
                schema.node_id = "KKEarlyEntry"
                return schema

            @classmethod
            def iter_chain_a(cls,*unused,**kwargs):
                result = ("", "{}", "frames-ready", *([None]*5), [image], manifest)
                yield result
                if not saved.wait(5):
                    raise RuntimeError("Image saving waited for final reverse")
                return ("finished", *result[1:])

        class ProbeSave(comfy_nodes.SaveImage):
            def save_images(self,images,filename_prefix="early",prompt=None,extra_pnginfo=None):
                result = super().save_images(images,filename_prefix,prompt,extra_pnginfo)
                saved.set()
                return result

        directory = Path(TEMP.name) / "early-saved"
        directory.mkdir()
        server = types.SimpleNamespace(client_id=None,last_node_id=None,send_sync=lambda *args:None)
        executor = PromptExecutor(server,cache_type=CacheType.CLASSIC,cache_args={"ram":0,"ram_inactive":0})
        prompt = {
            "source":{"class_type":"KKEarlySource","inputs":{}},
            "entry":{"class_type":"KKEarlyEntry","inputs":{"video":["source",0],"api_key":"offline",
                "review_api_key":"offline","requirement":"test","rules_text":"test"}},
            "reverse":{"class_type":"KKGO_VideoReverse","inputs":{"context":["entry",8]}},
            "save":{"class_type":"KKEarlySave","inputs":{"images":["entry",11],"filename_prefix":"early"}},
        }
        with patch.dict(comfy_nodes.NODE_CLASS_MAPPINGS,{"KKEarlySource":Source,"KKEarlyEntry":Entry,
                "KKGO_VideoReverse":stages.KKGO_VideoReverse,"KKEarlySave":ProbeSave}), \
                patch.object(folder_paths,"output_directory",str(directory)):
            await executor.execute_async(prompt,"early-frame-test",execute_outputs=["reverse","save"])
        self.assertTrue(executor.success)
        self.assertTrue(saved.is_set())
        self.assertEqual(len(list(directory.glob("early_*.png"))),1)

    def test_schema_preserves_old_output_slots(self):
        full = agent.KKGO_FullVideoAgent.define_schema()
        entry = stages.KKGO_VideoSegments.define_schema()
        reverse = stages.KKGO_VideoReverse.define_schema()
        self.assertEqual(len(full.outputs),15)
        self.assertTrue(full.outputs[8].is_output_list)
        self.assertEqual(len(entry.outputs),16)
        self.assertFalse(entry.outputs[8].is_output_list)
        self.assertTrue(entry.outputs[9].is_output_list)
        self.assertTrue(all(o.is_output_list for o in entry.outputs[11:16]))
        self.assertEqual(len(reverse.outputs),10)
        self.assertTrue(reverse.outputs[3].is_output_list)
        self.assertFalse(reverse.outputs[4].is_output_list)
        self.assertTrue(all(output.is_output_list for output in full.outputs[10:15]))
        self.assertTrue(all(output.is_output_list for output in reverse.outputs[5:10]))

    async def test_standard_save_image_receives_every_list_element(self):
        images = [torch.full((1,12,24,3),.1), torch.full((1,16,32,3),.5), torch.full((1,20,40,3),.9)]
        manifest = json.dumps({"frames":[{"segment_index":1},{"segment_index":1},{"segment_index":2}]})
        result = ("prompt", "{}", "ok", *([None]*5), images, manifest)

        class Source(io.ComfyNode):
            @classmethod
            def define_schema(cls):
                return io.Schema(node_id="KKImageTestSource", inputs=[], outputs=[stages.ChainContext.Output()])

            @classmethod
            def execute(cls):
                return io.NodeOutput(stages.VideoJob(result=result))

        directory = Path(TEMP.name) / "saved"
        directory.mkdir()
        server = types.SimpleNamespace(client_id=None, last_node_id=None, send_sync=lambda *unused: None)
        executor = PromptExecutor(server, cache_type=CacheType.CLASSIC, cache_args={"ram":0, "ram_inactive":0})
        prompt = {
            "source":{"class_type":"KKImageTestSource","inputs":{}},
            "reverse":{"class_type":"KKGO_VideoReverse","inputs":{"context":["source",0]}},
            "save":{"class_type":"SaveImage","inputs":{"images":["reverse",3],"filename_prefix":"all"}},
            "seg1":{"class_type":"SaveImage","inputs":{"images":["reverse",5],"filename_prefix":"seg1"}},
            "seg2":{"class_type":"SaveImage","inputs":{"images":["reverse",6],"filename_prefix":"seg2"}},
            "seg3":{"class_type":"SaveImage","inputs":{"images":["reverse",7],"filename_prefix":"seg3"}},
        }
        with patch.dict(comfy_nodes.NODE_CLASS_MAPPINGS, {"KKImageTestSource":Source,
                "KKGO_VideoReverse":stages.KKGO_VideoReverse}), patch.object(folder_paths,"output_directory",str(directory)):
            await executor.execute_async(prompt,"image-list-test",execute_outputs=["save","seg1","seg2","seg3"])
        self.assertTrue(executor.success)
        from PIL import Image
        files = sorted(directory.glob("all_*.png"))
        self.assertEqual(len(files),3)
        for path, expected in zip(files,[(24,12),(32,16),(40,20)]):
            with Image.open(path) as saved:
                self.assertEqual(saved.size,expected)
        cached = await executor.caches.outputs.get("reverse")
        self.assertEqual(cached.outputs[4],[manifest])
        self.assertEqual(len(cached.outputs[5]),2)
        self.assertEqual(len(cached.outputs[6]),1)
        self.assertTrue(all(len(group) == 1 and isinstance(group[0],ExecutionBlocker)
                            for group in cached.outputs[7:10]))
        self.assertEqual(len(list(directory.glob("seg1_*.png"))),2)
        self.assertEqual(len(list(directory.glob("seg2_*.png"))),1)
        self.assertEqual(len(list(directory.glob("seg3_*.png"))),0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
