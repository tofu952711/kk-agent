"""Exercise ComfyUI's real V3 plugin loader and preserve saved-workflow node IDs."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-registration-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "user")
sys.path.insert(0, os.environ.get("COMFYUI_PATH", str(ROOT.parents[1])))
sys.argv = [sys.argv[0]]
from comfy.cli_args import args
args.cpu = True
import comfy.model_management
import nodes


class RegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_v3_loader_preserves_node_ids_and_ports(self):
        self.assertTrue(await nodes.load_custom_node(str(ROOT)))
        expected = {"KKGO_FullVideoAgent": 15, "KKGO_VideoSegments": 9, "KKGO_VideoReverse": 10, "KKGO_VideoFrames": 7}
        for name, outputs in expected.items():
            cls = nodes.NODE_CLASS_MAPPINGS[name]
            schema = cls.GET_SCHEMA()
            self.assertEqual(schema.node_id, name)
            self.assertEqual(len(schema.outputs), outputs)
        module = sys.modules[str(ROOT).replace(".", "_x_")]
        self.assertFalse(hasattr(module, "NODE_CLASS_MAPPINGS"))
        self.assertTrue(callable(module.comfy_entrypoint))
        self.assertEqual(module.WEB_DIRECTORY, "./web")

    async def test_template_links_and_empty_credentials(self):
        graph = json.loads((ROOT / "example_workflows/KKagent_basic.json").read_text())
        by_id = {node["id"]: node for node in graph["nodes"]}
        for link_id, source, slot, target, target_slot, kind in graph["links"]:
            self.assertIn(link_id, by_id[source]["outputs"][slot]["links"])
            self.assertEqual(by_id[target]["inputs"][target_slot]["link"], link_id)
            self.assertEqual(by_id[source]["outputs"][slot]["type"], kind)
        widgets = by_id[2]["widgets_values"]
        cls = nodes.NODE_CLASS_MAPPINGS["KKGO_VideoSegments"]
        names = [item.id for item in cls.define_schema().inputs if hasattr(item, "default")]
        values = dict(zip(names, widgets))
        self.assertEqual(values["api_key"], "")
        self.assertEqual(values["review_api_key"], "")
        self.assertTrue(all(values[name].strip() for name in ["requirement", "rules_text", "segment_rules_text", "chain_b_reverse_rules_text"]))
        self.assertEqual(cls.define_schema().display_name, "kk agent 2")
        self.assertTrue(all(nodes.NODE_CLASS_MAPPINGS[name].define_schema().category == "kk agent 2" for name in ["KKGO_FullVideoAgent", "KKGO_VideoSegments", "KKGO_VideoReverse", "KKGO_VideoFrames"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
