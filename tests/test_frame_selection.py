"""Offline contract, PTS, full-resolution export and numbering regressions."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest

PLUGIN = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="kk-frame-tests-")
os.environ["KKGO_DATA_DIR"] = str(Path(TEMP.name) / "data")
package = types.ModuleType("kk_frame_tests")
package.__path__ = [str(PLUGIN)]
sys.modules[package.__name__] = package
frames = importlib.import_module(package.__name__ + ".kkagent_bridge.frame_selection")
media = importlib.import_module(package.__name__ + ".kkagent_bridge.media_tools")


def response(count=1, frequent=False):
    return {"targets": ["模特", "背景", "产品"],
        "reference_bindings": [{"slot": i, "role": role} for i, role in enumerate(["模特", "背景", "产品"],1)],
        "selected_frames": [{"candidate_id": f"C{i:03d}", "visible_targets": ["模特", "背景", "产品"],
            "clear_targets": ["模特", "背景", "产品"], "at_edit_boundary": True,
            "subject_positions": "人物中间持产品，背景可辨", "reason": "清晰同框，适合替换编辑"} for i in range(1,count+1)],
        "shot_analysis":{"mode":"edited" if frequent else "fixed", "scene_unchanged":not frequent,
            "confirmed_changes":[{"before_candidate_id":a,"after_candidate_id":b,"reason":"测试前后构图切换"}
                for a,b in (("C001","C002"),("C003","C004"))] if frequent else [],"reason":"测试镜头结构"},
        "opening_assessment":[{"candidate_id":"C001","clear":True,"reason":"首帧清晰"}],
        "unmet_targets": [], "notes": "无"}


class SelectionContractTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"candidate_id": f"C{i:03d}", "source_time": i/10} for i in range(1,7)]

    def parse(self, value):
        return frames.parse_selection(json.dumps(value, ensure_ascii=False), self.candidates, [1,2,3])

    def test_fixed_camera_keeps_only_opening(self):
        self.assertEqual(len(self.parse(response(1))["selected_frames"]), 1)
        self.assertTrue(self.parse(response(1))["joint_clear_frame"])
        for count in (2,3,4,5):
            with self.assertRaises(ValueError):
                self.parse(response(count))

    def test_edited_video_allows_two_to_five(self):
        for count in (2,3,4,5):
            self.assertEqual(len(self.parse(response(count,frequent=True))["selected_frames"]),count)
        with self.assertRaises(ValueError):
            self.parse(response(1,frequent=True))

    def test_one_clear_edit_allows_two_to_five(self):
        value=response(3,frequent=True)
        value["shot_analysis"]["confirmed_changes"].pop()
        self.assertEqual(len(self.parse(value)["selected_frames"]),3)
        value["shot_analysis"]["mode"]="fixed"
        with self.assertRaisesRegex(ValueError,"不一致"):
            self.parse(value)
        value["shot_analysis"]["mode"]="edited"
        value["selected_frames"]=value["selected_frames"][:2]
        self.assertEqual(len(self.parse(value)["selected_frames"]),2)
        value["selected_frames"]=value["selected_frames"][:1]
        with self.assertRaises(ValueError):
            self.parse(value)

    def test_no_edit_evidence_cannot_request_edited_mode(self):
        value=response()
        value["shot_analysis"]["mode"]="edited"
        with self.assertRaisesRegex(ValueError,"不一致"):
            self.parse(value)

    def test_same_scene_edits_keep_only_opening(self):
        value=response(1,frequent=True)
        value["shot_analysis"].update(mode="fixed", scene_unchanged=True,
            reason="同一房间同一背景，只有同场景跳剪和景别切换")
        self.assertEqual(len(self.parse(value)["selected_frames"]), 1)
        value["shot_analysis"]["mode"]="edited"
        with self.assertRaisesRegex(ValueError,"不一致"):
            self.parse(value)
        value=response(2,frequent=True)
        value["shot_analysis"].update(mode="fixed",scene_unchanged=True)
        with self.assertRaisesRegex(ValueError,"一直未变只保留1张"):
            self.parse(value)

    def test_scene_change_requires_evidence(self):
        value=response(2,frequent=True)
        value["shot_analysis"]["confirmed_changes"]=[]
        with self.assertRaisesRegex(ValueError,"场景变化须提供"):
            self.parse(value)

    def test_scene_judgement_must_be_explicit_boolean(self):
        for state in (None,"true",0):
            value=response()
            value["shot_analysis"]["scene_unchanged"]=state
            with self.assertRaises(ValueError):
                self.parse(value)

    def test_duplicate_or_unverified_cut_pairs_rejected(self):
        for pair in (("C001","C003"),("C009","C010"),("C001","C002")):
            value=response(3,frequent=True)
            value["shot_analysis"]["confirmed_changes"][1].update(before_candidate_id=pair[0],after_candidate_id=pair[1])
            with self.assertRaisesRegex(ValueError,"前后对照"):
                self.parse(value)

    def test_six_is_rejected_without_truncation(self):
        with self.assertRaises(ValueError):
            self.parse(response(6))

    def test_empty_is_not_a_successful_selection(self):
        with self.assertRaises(ValueError):
            self.parse(response(0))

    def test_invalid_or_duplicate_candidate_is_rejected(self):
        for cid in ("missing", "C001"):
            value = response(3,frequent=True)
            value["selected_frames"][1]["candidate_id"] = cid
            with self.assertRaises(ValueError):
                self.parse(value)

    def test_unmet_targets_cannot_be_hidden(self):
        value = response()
        for item in value["selected_frames"]:
            item["visible_targets"] = ["模特", "背景"]
            item["clear_targets"] = ["模特", "背景"]
        with self.assertRaisesRegex(ValueError, "未覆盖"):
            self.parse(value)
        value["unmet_targets"] = ["产品"]
        self.assertFalse(self.parse(value)["joint_clear_frame"])

    def test_complementary_frames_need_no_fabricated_joint_frame(self):
        value = response(3,frequent=True)
        value["selected_frames"][0].update(visible_targets=["模特", "背景"], clear_targets=["模特", "背景"])
        value["selected_frames"][1].update(visible_targets=["产品"], clear_targets=["产品"])
        value["selected_frames"][2].update(visible_targets=["背景"], clear_targets=["背景"])
        self.assertFalse(self.parse(value)["joint_clear_frame"])

    def test_clear_opening_frame_cannot_be_skipped(self):
        value = response(1)
        self.assertTrue(self.parse(value)["opening_frame"]["first_decoded_frame_used"])
        for i, item in enumerate(value["selected_frames"],2):
            item["candidate_id"] = f"C{i:03d}"
        value["opening_assessment"] = [
            {"candidate_id":"C001","clear":True,"reason":"首帧清晰"},
            {"candidate_id":"C002","clear":True,"reason":"后一帧也清晰"}]
        with self.assertRaisesRegex(ValueError,"最早清晰"):
            self.parse(value)

    def test_unclear_opening_uses_earliest_clear_candidate(self):
        value = response(1)
        for i, item in enumerate(value["selected_frames"],3):
            item["candidate_id"] = f"C{i:03d}"
        value["opening_assessment"] = [
            {"candidate_id":"C001","clear":False,"reason":"黑场"},
            {"candidate_id":"C002","clear":False,"reason":"叠化"},
            {"candidate_id":"C003","clear":True,"reason":"最早清晰原帧"}]
        parsed = self.parse(value)
        self.assertFalse(parsed["opening_frame"]["first_decoded_frame_used"])
        self.assertEqual(parsed["opening_frame"]["candidate_id"],"C003")
        value["opening_assessment"].pop(1)
        with self.assertRaisesRegex(ValueError,"不得跳过"):
            self.parse(value)

    def test_unconnected_reference_is_rejected(self):
        value = response()
        value["reference_bindings"][0]["slot"] = 9
        with self.assertRaises(ValueError):
            self.parse(value)

    def test_per_generation_numbering_resets(self):
        selections = [{"segment_index":1, "frames":[{},{}]}, {"segment_index":2, "frames":[{}]}]
        result = frames.assign_image_numbers(selections, [1,2,3])
        self.assertEqual([r["image_number"] for r in result], [4,5,4])
        self.assertEqual([r["output_list_index"] for r in result], [1,2,3])
        self.assertEqual([r["segment_image_index"] for r in result], [1,2,1])

    def test_sparse_and_absent_user_inputs(self):
        for slots, expected in (([1,8],9), ([],1)):
            result = frames.assign_image_numbers([{"segment_index":1,"frames":[{}]}], slots)
            self.assertEqual(result[0]["image_number"], expected)

    def test_mandatory_mentions_and_cross_segment_bounds(self):
        selected = [{"image_number":4}, {"image_number":5}]
        frames.validate_mentions("0秒按编辑后的@图片4，2秒按@图片5；人物以@图片1为准", selected, [1,2,3])
        for text in ("缺少引用", "仅有@图片4", "@图片4 @图片5 @图片6", "@图片4 @图片50"):
            with self.assertRaises(ValueError):
                frames.validate_mentions(text, selected, [1,2,3])


class RealMediaTests(unittest.TestCase):
    def check_video(self, rate, offset=False, anamorphic=False):
        from PIL import Image
        import numpy as np
        with tempfile.TemporaryDirectory(prefix="kk-real-frames-") as temporary:
            root = Path(temporary)
            source = root / "source-video.mp4"
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                f"testsrc2=size=640x360:rate={rate}:duration=4"]
            if anamorphic:
                command += ["-vf", "setsar=2"]
            command += ["-c:v", "libx264", "-threads", "1"]
            if offset:
                command += ["-output_ts_offset", "5"]
            subprocess.run(command + [str(source)], check=True)
            output = root / "analysis"
            tools = media.MediaTools(source, output)
            info = tools.info()
            # A cut near a frame boundary and a late target exercise local PTS.
            tools._write_json("shot_candidates.json", {"candidate_cuts": [{"time":2.4}]})
            segment = {"start":2., "end": min(4., info["duration_seconds"])}
            directory, candidates, sheets = frames.prepare_candidates(output, {"video":info}, 2, segment)
            self.assertTrue(candidates and sheets)
            import base64
            self.assertTrue(base64.b64decode(sheets[0]).startswith(b"\x89PNG"))
            self.assertTrue(all(segment["start"] <= c["source_time"] < segment["end"] for c in candidates))
            self.assertTrue(all(0 <= c["segment_position"] < 1 for c in candidates))
            middle = min(candidates,key=lambda c:abs(c["segment_position"]-.5))
            self.assertLess(abs(middle["segment_position"]-.5),1/info["fps"]/(segment["end"]-segment["start"])+1e-6)
            opening = [c for c in candidates if c["local_time"] < .95]
            self.assertGreaterEqual(len(opening),20)
            # The assigned time can fall between decoded PTS at fractional FPS.
            self.assertGreaterEqual(candidates[0]["local_time"],0.)
            self.assertLess(candidates[0]["local_time"],1/info["fps"]+1e-6)
            selected = [{"candidate_id": candidates[0]["candidate_id"]}, {"candidate_id": candidates[-1]["candidate_id"]}]
            result = frames.materialize_selection(source, directory, {"selected_frames":selected}, candidates, segment, 2)
            tensors = frames.load_output_images(result)
            self.assertEqual(len(tensors), 2)
            for record, tensor in zip(result, tensors):
                self.assertEqual(tuple(tensor.shape), (1,360,640,3))
                self.assertEqual(record["local_time"], round(record["source_time"] - 2.,6))
                with Image.open(record["file"]) as saved:
                    self.assertEqual(saved.size, (640,360))
                self.assertEqual(tensor.numpy().dtype, np.float32)
            # Different selected PTS produce different real pixels, no duplicated fallback.
            self.assertFalse(np.array_equal(tensors[0].numpy(), tensors[1].numpy()))
            def cancelled():
                raise RuntimeError("cancel-probe")
            with self.assertRaisesRegex(RuntimeError, "cancel-probe"):
                frames.prepare_candidates(output, {"video":info}, 3, segment, cancelled)
            self.assertFalse(list((output / "b-frame-selection" / "segment-3").glob("cached-frame-*")))

    def test_fractional_frame_rate_and_full_resolution(self):
        self.check_video("30000/1001")

    def test_nonzero_source_pts_are_normalized(self):
        self.check_video("25", offset=True)

    def test_pixel_resolution_is_not_resized_for_sample_aspect_ratio(self):
        self.check_video("25", anamorphic=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
