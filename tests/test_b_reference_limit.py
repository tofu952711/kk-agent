"""Real FFmpeg regression: short reference outputs keep full B analysis ranges."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get("COMFYUI_PATH", str(ROOT.parents[1])))
sys.path.insert(0, str(ROOT))
from comfy.cli_args import args
args.cpu = True
from kkagent_bridge.ffmpeg_segments import executables, cut_tool_videos
from kkagent_bridge.chain_b import read_tool_videos
from kkagent_bridge.media_tools import MediaTools
from kkagent_bridge.b_segment_media import prepare_segment_bundle


class ReferenceLimitTests(unittest.TestCase):
    def check_case(self, cut, fps, audio):
        with tempfile.TemporaryDirectory(prefix="kk-b-reference-test-") as temporary:
            root = Path(temporary)
            source = root / "source-video.mp4"
            output = root / "media-analysis"
            output.mkdir()
            ffmpeg, ffprobe = executables()
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                       f"testsrc2=size=64x96:rate={fps}:duration=50"]
            if audio:
                command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=50", "-c:a", "aac"]
            command += ["-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(source)]
            subprocess.run(command, check=True, capture_output=True)
            tools = MediaTools(source, output)
            info = tools.info()
            ranges = [{"start": 0., "end": cut}, {"start": cut, "end": info["duration_seconds"]}]
            plan = cut_tool_videos(source, output, info, ranges, keep_audio=audio)
            tools._write_json("video_segments.json", plan)
            original = {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in plan["files"]}
            videos, record = read_tool_videos(output, info)
            self.assertEqual(record["segments"], plan["segments"])
            self.assertEqual(record["analysis_actual_durations"], plan["actual_durations"])
            self.assertEqual(sum(plan["exported_frames"]), plan["source_frames"])
            for index, (video, segment, reference) in enumerate(zip(videos, plan["segments"], record["reference_videos"]), 1):
                self.assertLessEqual(video.get_duration(), 30.0)
                saved = root / f"saved-{index}.mp4"
                video.save_to(str(saved))
                probe = json.loads(subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format",
                    "-of", "json", str(saved)], check=True, capture_output=True, text=True).stdout)
                durations = [float(probe["format"]["duration"])] + [float(s["duration"]) for s in probe["streams"] if "duration" in s]
                self.assertLessEqual(max(durations), 30.0)
                self.assertEqual(any(s["codec_type"] == "audio" for s in probe["streams"]), audio)
                self.assertEqual((reference["analysis_start"], reference["analysis_end"]), (segment["start"], segment["end"]))
                if reference["trimmed"]:
                    self.assertGreater(reference["discarded_reference_tail_seconds"], 0)
                    times = [segment["start"] + .1, segment["end"] - .1]
                    tools.cache_frames(times, sequential=True)
                    meta, _ = tools.sheet(times)
                    shots = tools.shots()
                    tools._write_json("transcript.json", {"tokens": ["尾部"], "timestamps": [times[-1]],
                        "full_text": "尾部", "segments": []})
                    report = {"video": info, "sheets": [{"kind": "完整分段证据", **meta}],
                              "voice_evidence": {"windows": [], "change_candidates": []}}
                    directory, text, sheets, scope = prepare_segment_bundle(output, report, [], index, segment, 2, {}, reference=reference)
                    self.assertEqual(scope["end"], segment["end"])
                    self.assertIn("提示词仍须描述完整本段", text)
                    self.assertTrue(sheets)
                    self.assertEqual(json.loads((directory / "transcript.json").read_text(encoding="utf-8"))["tokens"], ["尾部"])
                    frames = json.loads((directory / "frame_index.json").read_text(encoding="utf-8"))
                    self.assertGreater(max(f["actual"] for f in frames.values()), reference["reference_source_end"])
            self.assertEqual(original, {name: hashlib.sha256((output / name).read_bytes()).hexdigest() for name in plan["files"]})
            self.assertEqual(json.loads((output / "video_segments.json").read_text(encoding="utf-8")), plan)

    def test_long_first_segment_with_audio(self):
        self.check_case(30.5, "30", True)

    def test_long_last_segment_fractional_fps_without_audio(self):
        self.check_case(18.5, "30000/1001", False)

    def test_exactly_30_seconds_unchanged(self):
        self.check_case(30., "30", False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
