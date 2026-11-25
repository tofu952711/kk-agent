"""Source installs, checksummed assets and legacy/shared model path regressions."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assets = load("kk_install_test_assets", ROOT / "scripts/model_assets.py")
paths = load("kk_install_test_paths", ROOT / "kkagent_bridge/model_paths.py")
installer = load("kk_install_test_hook", ROOT / "install.py")


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.comfy = self.root / "ComfyUI"
        (self.comfy / "models").mkdir(parents=True)
        (self.comfy / "folder_paths.py").write_text("")
        self.package = self.comfy / "custom_nodes/plugin"
        (self.package / "kkagent_bridge").mkdir(parents=True)
        (self.package / "kkagent_bridge/model_paths.py").write_bytes((ROOT / "kkagent_bridge/model_paths.py").read_bytes())
        self.clean_env = patch.dict(os.environ, {}, clear=True)
        self.clean_env.start()
        self.addCleanup(self.clean_env.stop)

    def populate(self, directory, group="sensevoice"):
        directory.mkdir(parents=True, exist_ok=True)
        for name in paths.GROUPS[group][2]:
            (directory / name).write_bytes(name.encode())
        return directory

    def asset(self, name="model.int8.onnx", payload=b"test-model"):
        source = self.root / (name + ".source")
        source.write_bytes(payload)
        return {"group": "sensevoice", "name": name, "url": source.as_uri(),
                "sha256": hashlib.sha256(payload).hexdigest()}

    def test_new_source_install_downloads_to_shared_models_without_manifest_or_vendor(self):
        records = assets.install_models(self.comfy, package_dir=self.package,
                                      assets=[self.asset(), self.asset("tokens.txt", b"tokens")])
        directory = self.comfy / "models/kkagent/sensevoice"
        self.assertTrue((directory / "model.int8.onnx").is_file())
        self.assertEqual(paths.model_directory("sensevoice", comfy_root=self.comfy, package_dir=self.package), directory)
        self.assertTrue(all(record["status"] == "downloaded" for record in records))
        self.assertFalse((self.package / "bundle-manifest.json").exists())
        self.assertFalse((self.package / "vendor").exists())

    def test_legacy_models_reused_without_download_or_duplicate(self):
        legacy = self.populate(self.package / "models/sensevoice")
        items = [self.asset(name, name.encode()) for name in paths.GROUPS["sensevoice"][2]]
        with patch.object(assets, "download", side_effect=AssertionError("unexpected download")):
            records = assets.install_models(self.comfy, package_dir=self.package, assets=items)
        self.assertTrue(all(Path(record["path"]).parent == legacy for record in records))
        self.assertFalse((self.comfy / "models/kkagent/sensevoice").exists())

    def test_partial_legacy_group_is_copied_into_one_complete_shared_folder(self):
        legacy = self.package / "models/sensevoice"
        legacy.mkdir(parents=True)
        (legacy / "model.int8.onnx").write_bytes(b"test-model")
        assets.install_models(self.comfy, package_dir=self.package,
                              assets=[self.asset(), self.asset("tokens.txt", b"tokens")])
        selected = paths.model_directory("sensevoice", comfy_root=self.comfy, package_dir=self.package)
        self.assertTrue(all((selected / name).exists() for name in paths.GROUPS["sensevoice"][2]))

    def test_shared_complete_group_preferred_over_legacy(self):
        self.populate(self.package / "models/sensevoice")
        shared = self.populate(self.comfy / "models/kkagent/sensevoice")
        self.assertEqual(paths.model_directory("sensevoice", comfy_root=self.comfy, package_dir=self.package), shared)

    def test_explicit_environment_override_preserved(self):
        override = self.populate(self.root / "configured")
        with patch.dict(os.environ, {"KKGO_ASR_MODEL_DIR": str(override)}):
            self.assertEqual(paths.model_directory("sensevoice", comfy_root=self.comfy, package_dir=self.package), override)

    def test_extra_model_paths_registered_and_resolved(self):
        extra = self.populate(self.root / "external")
        registered = {"kkagent_sensevoice": [str(extra)], "kkagent_speaker": []}
        fake = types.SimpleNamespace(models_dir=str(self.comfy / "models"),
            add_model_folder_path=lambda key, value: registered.setdefault(key, []).append(value)
                if value not in registered.setdefault(key, []) else None,
            get_folder_paths=lambda key: registered[key])
        with patch.dict(sys.modules, {"folder_paths": fake}):
            self.assertEqual(paths.model_directory("sensevoice", comfy_root=self.comfy, package_dir=self.package), extra)

    def test_corrupt_existing_model_never_overwritten(self):
        directory = self.populate(self.comfy / "models/kkagent/sensevoice")
        before = (directory / "model.int8.onnx").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            assets.install_models(self.comfy, package_dir=self.package, assets=[self.asset()])
        self.assertEqual((directory / "model.int8.onnx").read_bytes(), before)

    def test_bad_download_hash_leaves_no_model_or_partial_file(self):
        item = self.asset()
        item["sha256"] = "0" * 64
        target = self.root / "download/model.onnx"
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            assets.download(item, target)
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.iterdir()), [])

    def test_archive_extracts_only_named_regular_model_and_checks_both_hashes(self):
        archive_path = self.root / "model.tar.bz2"
        payload = b"onnx-fixture"
        with tarfile.open(archive_path, "w:bz2") as archive:
            for name in ("bundle/model.onnx", "../../unwanted"):
                entry = tarfile.TarInfo(name)
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
        item = {"name": "segmentation.onnx", "url": archive_path.as_uri(),
                "member": "bundle/model.onnx", "sha256": hashlib.sha256(payload).hexdigest(),
                "archive_sha256": assets.sha256(archive_path)}
        target = self.root / "safe/model.onnx"
        assets.download(item, target)
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse((self.root / "unwanted").exists())
        item["archive_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "archive checksum"):
            assets.download(item, self.root / "safe/bad.onnx")

    def test_offline_missing_models_never_downloads(self):
        with patch.object(assets, "download", side_effect=AssertionError("network in offline mode")):
            with self.assertRaises(FileNotFoundError):
                assets.install_models(self.comfy, package_dir=self.package, offline=True, assets=[self.asset()])

    def test_unreachable_origin_uses_mirror_with_same_expected_hash(self):
        asset = self.asset()
        asset["mirrors"] = [asset["url"]]
        asset["url"] = (self.root / "missing-origin").as_uri()
        destination = self.root / "mirror/model.onnx"
        assets.download(asset, destination)
        self.assertEqual(assets.sha256(destination), asset["sha256"])

    def test_manager_finds_root_with_changed_working_directory(self):
        with patch.object(installer, "__file__", str(self.package / "install.py")), patch("pathlib.Path.cwd", return_value=self.package):
            self.assertEqual(installer.find_comfy_root(), self.comfy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
