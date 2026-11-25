"""Run the real install CLI from a fresh source tree with local asset fixtures."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMFY = Path(os.environ.get("COMFYUI_PATH", str(ROOT.parents[1])))


class SourceInstallTests(unittest.TestCase):
    def test_fresh_source_cli_requires_no_bundle_and_keeps_existing_python_environment(self):
        with tempfile.TemporaryDirectory(prefix="kk-fresh-install-") as work:
            work = Path(work)
            comfy = work / "ComfyUI"
            (comfy / "models").mkdir(parents=True)
            shutil.copy2(COMFY / "folder_paths.py", comfy / "folder_paths.py")
            for name in ("comfy", "utils"):
                (comfy / name).symlink_to(COMFY / name, target_is_directory=True)
            package = comfy / "custom_nodes/kk-agent"
            package.mkdir(parents=True)
            for name in ("install.py",):
                shutil.copy2(ROOT / name, package / name)
            for name in ("scripts", "kkagent_bridge"):
                shutil.copytree(ROOT / name, package / name, ignore=shutil.ignore_patterns("__pycache__"))
            fixture = work / "fixtures"
            fixture.mkdir()
            definitions = []
            for group, names in (("sensevoice", ("model.int8.onnx", "tokens.txt")),
                                 ("speaker-diarization", ("segmentation.onnx", "speaker_embedding.onnx"))):
                for name in names:
                    path = fixture / name
                    path.write_bytes(("fixture:" + name).encode())
                    definitions.append({"group": group, "name": name, "url": path.as_uri(),
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            module = package / "scripts/model_assets.py"
            with module.open("a") as stream:
                stream.write("\nASSETS = " + repr(definitions) + "\n")
            environment = {key: value for key, value in os.environ.items() if not key.startswith("KKGO_")}
            environment["KKGO_DATA_DIR"] = str(work / "records")
            before = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], env=environment)
            result = subprocess.run([sys.executable, str(package / "install.py"), "--comfy-root", str(comfy)],
                                    cwd=package, env=environment, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("installation verified", result.stdout)
            self.assertFalse((package / "bundle-manifest.json").exists())
            self.assertFalse((package / "vendor").exists())
            for model in definitions:
                path = comfy / "models/kkagent" / model["group"] / model["name"]
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), model["sha256"])
            after = subprocess.check_output([sys.executable, "-m", "pip", "freeze"], env=environment)
            self.assertEqual(before, after, "Source install modified the Python environment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
