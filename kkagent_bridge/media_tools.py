"""Local, bounded video tools. No model credentials or arbitrary commands."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from pathlib import Path
import time


class MediaTools:
    def __init__(self, video: Path, output: Path, model_dir: Path | None = None):
        self.video = video.resolve(strict=True)
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.model_dir = model_dir
        self._recognizer = None
        self._speaker_analyzer = None
        self._cache = {}
        # The staged source and output are private and immutable for one job.
        # This disk cache is also read by the later MCP subprocess.
        self._frame_index = self._read_json("frame_index.json") or {}

    def _read_json(self, name):
        path = self.output / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _write_json(self, name, value):
        path = self.output / name
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _time_key(value):
        return float(value).hex()

    def info(self):
        if "info" in self._cache:
            return self._cache["info"]
        import av
        with av.open(str(self.video)) as container:
            stream = container.streams.video[0]
            duration = float(stream.duration * stream.time_base) if stream.duration else float(container.duration or 0) / 1e6
            result = {"duration_seconds": duration, "fps": float(stream.average_rate or 0),
                    "frames": stream.frames, "width": stream.width, "height": stream.height,
                    "audio_tracks": len(container.streams.audio), "asr_available": self.asr_available(),
                    "speaker_analysis": self.speaker_availability(),
                    "timing_note": "视频时间为原始 PTS；镜头检测是候选，须用切点前后帧复核。"}
        if duration <= 0 or not math.isfinite(duration):
            raise ValueError("无法确定视频时长")
        self._cache["info"] = result
        return result

    def asr_available(self):
        return bool(self.model_dir and all((self.model_dir / n).is_file() for n in ("model.int8.onnx", "tokens.txt")))

    def set_video_segments(self, segments):
        policy = self._read_json("duration_policy.json")
        if policy and policy.get("phase") != "segment":
            raise ValueError("当前为反推阶段或短视频路径，插件时长策略禁止修改视频分段")
        chain_b = bool(policy and policy.get("chain") == "B")
        if chain_b and (not isinstance(segments, list) or not 2 <= len(segments) <= 5):
            raise ValueError("链路B必须提交2—5段连续覆盖全片的方案；段数和切点由本阶段分段规定决定")
        if policy and not chain_b and (not isinstance(segments, list) or len(segments) != 2):
            raise ValueError("超过31.9秒且不超过60秒视频必须分成两段，最多两段；请提交两段连续覆盖全片的方案")
        try:
            from .video_segments import validate_segments
        except ImportError:
            from video_segments import validate_segments
        info = self.info()
        selected = validate_segments(segments, info["duration_seconds"], info["fps"])
        try:
            from .segment_contract import validate_requested_count
        except ImportError:
            from segment_contract import validate_requested_count
        validate_requested_count(selected, self._read_json("segment_request.json"))
        if chain_b:
            # This is the actual AI-invoked tool action, not a plan for the
            # parent node to execute later. Publish only fully cut results.
            try:
                from .ffmpeg_segments import cut_tool_videos
            except ImportError:
                from ffmpeg_segments import cut_tool_videos
            result = cut_tool_videos(self.video, self.output, info, selected,
                keep_audio=policy.get("keep_audio", True), timeout=policy.get("tool_timeout_s", 900))
            self._write_json("video_segments.json", result)
            return result
        result = {"segments": selected, "count": len(selected), "status": "planned",
                  "note": ("两段方案已保存。请结束分段规划；插件将先实际裁切，完成后独立执行反推任务。"
                           if policy else "方案已保存。最终视频由节点输出。")}
        self._write_json("video_segments.json", result)
        return result

    @staticmethod
    def speaker_availability():
        try:
            from .speaker_analysis import availability
        except ImportError:
            from speaker_analysis import availability
        return availability()

    def diarize_speakers(self, num_speakers=0, threshold=.5):
        try:
            from .speaker_analysis import SpeakerAnalyzer, settings, validate_options
        except ImportError:
            from speaker_analysis import SpeakerAnalyzer, settings, validate_options
        validate_options(num_speakers, threshold)
        config = settings()
        if (self._speaker_analyzer is None or self._speaker_analyzer.directory != config["model_dir"]
                or self._speaker_analyzer.provider != config["provider"]):
            self._speaker_analyzer = SpeakerAnalyzer(config["model_dir"], config["provider"])
        analyzer = self._speaker_analyzer
        cache_key = {"models": analyzer.fingerprint(), "num_speakers": num_speakers, "threshold": threshold}
        saved = self._read_json("speaker_evidence.json")
        if saved is not None and saved.get("cache_key") == cache_key:
            return saved
        audio, offset = self.audio_samples()
        result = analyzer.analyze(audio, offset=offset, num_speakers=num_speakers, threshold=threshold)
        result["cache_key"] = cache_key
        self._write_json("speaker_evidence.json", result)
        return result

    def shots(self, threshold=0.16):
        import av
        import numpy as np
        threshold = float(threshold)
        if not 0.05 <= threshold <= 0.5:
            raise ValueError("threshold 必须为0.05–0.5")
        cache_key = f"shots-{threshold}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        saved = self._read_json("shot_candidates.json")
        if saved and saved.get("threshold") == threshold:
            self._cache[cache_key] = saved
            return saved
        candidates = []
        previous = None
        scores = []
        with av.open(str(self.video)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "SLICE"
            stream.codec_context.thread_count = 2
            origin = float(stream.start_time or 0) * float(stream.time_base)
            for index, frame in enumerate(container.decode(stream)):
                # Resize during pixel conversion; do not allocate a full-size
                # PIL image for every source frame just to compare 96x96 pixels.
                array = frame.reformat(width=96, height=96, format="rgb24").to_ndarray().astype(np.float32) / 255.0
                hist = np.concatenate([np.histogram(array[:, :, c], bins=16, range=(0, 1))[0] / 9216 for c in range(3)])
                timestamp = max(0, float(frame.pts * stream.time_base) - origin) if frame.pts is not None else index / float(stream.average_rate)
                if previous is not None:
                    pixel = float(np.abs(array - previous[0]).mean())
                    histogram = float(np.abs(hist - previous[1]).sum() / 6)
                    score = pixel * 0.65 + histogram * 0.35
                    scores.append((timestamp, score))
                    if score >= threshold and (not candidates or timestamp - candidates[-1]["time"] >= 0.15):
                        candidates.append({"time": round(timestamp, 6), "frame": index,
                                           "score": round(score, 4), "verified": False})
                previous = (array, hist)
        duration = self.info()["duration_seconds"]
        boundaries = [0] + [c["time"] for c in candidates] + [duration]
        report = {"method": "相邻原始帧RGB差异与颜色直方图变化；非语义镜头识别", "threshold": threshold,
                  "candidate_cuts": candidates, "candidate_intervals": [
                      {"index": i + 1, "start": a, "end": round(b, 3)}
                      for i, (a, b) in enumerate(zip(boundaries, boundaries[1:]))],
                  "top_changes": [{"time": round(t, 3), "score": round(s, 4)} for t, s in sorted(scores, key=lambda x: x[1], reverse=True)[:40]],
                  "warning": "候选可能包含快速运动，渐变或相近构图切镜可能漏检；用get_frames补看前后帧。不能把抽帧间隔当切镜。"}
        self._cache[cache_key] = report
        self._write_json("shot_candidates.json", report)
        return report

    def _validate_times(self, times, limit=None):
        times = sorted(set(float(t) for t in times))
        duration = self.info()["duration_seconds"]
        if not times or (limit and len(times) > limit) or any(not math.isfinite(t) or t < 0 or t >= duration for t in times):
            raise ValueError("帧时间须为有限数，范围0≤time<视频时长；交互补帧每批1–12张")
        return times

    def cache_frames(self, times, *, sequential=False):
        """One sequential decode for preparation, accurate seeking for details."""
        import av
        times = self._validate_times(times)
        missing = [t for t in times if self._time_key(t) not in self._frame_index]
        if not missing:
            return
        with av.open(str(self.video)) as container:
            stream = container.streams.video[0]
            stream.thread_type = "SLICE"
            stream.codec_context.thread_count = 2
            origin = float(stream.start_time or 0) * float(stream.time_base)
            fps = float(stream.average_rate or 30)
            # Respect display aspect ratio (e.g. anamorphic sources).
            sar = float(stream.sample_aspect_ratio or 1)
            aspect = stream.width * sar / stream.height
            width, height = (round(720 * aspect), 720) if aspect < 1 else (720, round(720 / aspect))

            def save(frame, target, timestamp):
                key = self._time_key(target)
                name = "cached-frame-" + hashlib.sha256(key.encode()).hexdigest()[:16] + ".jpg"
                frame.reformat(width=max(1, width), height=max(1, height), format="rgb24").to_image().save(
                    self.output / name, "JPEG", quality=93)
                self._frame_index[key] = {"file": name, "requested": target, "actual": round(timestamp, 6)}

            if sequential:
                target = 0
                for index, frame in enumerate(container.decode(stream)):
                    timestamp = float(frame.pts * stream.time_base) - origin if frame.pts is not None else index / fps
                    while target < len(missing) and timestamp + 1e-6 >= missing[target]:
                        save(frame, missing[target], timestamp)
                        target += 1
                    if target == len(missing):
                        break
            else:
                for t in missing:
                    container.seek(int((t + origin) / float(stream.time_base)), stream=stream, backward=True, any_frame=False)
                    for frame in container.decode(stream):
                        if frame.pts is None:
                            raise ValueError("补帧需要可定位的原始视频PTS")
                        timestamp = float(frame.pts * stream.time_base) - origin
                        if timestamp + 1e-6 >= t:
                            save(frame, t, timestamp)
                            break
        self._write_json("frame_index.json", self._frame_index)
        if any(self._time_key(t) not in self._frame_index for t in times):
            raise ValueError("请求时间超出可解码视频范围，请稍向前取帧")

    def sheet(self, times, *, cols=4, prefix="frames", detail=False):
        from PIL import Image, ImageDraw, ImageFont
        times = list(times)  # caller order pairs cut-before / cut-after
        records = [self._frame_index[self._time_key(t)] for t in times]
        with Image.open(self.output / records[0]["file"]) as first:
            scale = (720 if detail else 356) / max(first.size)
            tile_width, tile_height = max(1, round(first.width * scale)), max(1, round(first.height * scale))
        cols = min(cols, len(times))
        label = 28
        sheet = Image.new("RGB", (cols * tile_width, math.ceil(len(times) / cols) * (tile_height + label)), "#262626")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=17)
        for index, record in enumerate(records):
            x, y = index % cols * tile_width, index // cols * (tile_height + label)
            with Image.open(self.output / record["file"]) as image:
                image.thumbnail((tile_width, tile_height))
                sheet.paste(image, (x, y + label))
            draw.text((x + 3, y + 4), f"{index+1}: {record['actual']:.3f}s", fill="white", font=font)
        raw = io.BytesIO()
        sheet.save(raw, "JPEG", quality=92)
        name = prefix + "-" + hashlib.sha256(json.dumps([times, cols, detail]).encode()).hexdigest()[:12] + ".jpg"
        (self.output / name).write_bytes(raw.getvalue())
        return {"requested_times": times, "actual_times": [r["actual"] for r in records],
                "layout": f"{cols}列，按行从左到右；标签是原视频采样时间，不是确认切点", "file": name}, raw.getvalue()

    def frames(self, times):
        times = self._validate_times(times, limit=12)
        self.cache_frames(times)
        return self.sheet(times, cols=1 if len(times) == 1 else 4, detail=len(times) == 1)

    def audio_samples(self):
        if "audio" in self._cache:
            return self._cache["audio"]
        import av
        import numpy as np
        arrays = []
        with av.open(str(self.video)) as container:
            if not container.streams.audio:
                return np.zeros(0, dtype=np.float32), 0
            stream = container.streams.audio[0]
            origin = float(stream.start_time or 0) * float(stream.time_base)
            video_stream = container.streams.video[0]
            video_origin = float(video_stream.start_time or 0) * float(video_stream.time_base)
            resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
            for frame in container.decode(stream):
                arrays.extend(output.to_ndarray().reshape(-1) for output in resampler.resample(frame))
            arrays.extend(output.to_ndarray().reshape(-1) for output in resampler.resample(None))
        result = np.concatenate(arrays) if arrays else np.zeros(0, dtype=np.float32), origin - video_origin
        self._cache["audio"] = result
        return result

    def analyze_voices(self, start=None, end=None):
        try:
            from .voice_analysis import analyze
        except ImportError:  # MCP script entry point
            from voice_analysis import analyze
        whole = start is None and end is None
        if whole:
            saved = self._read_json("voice_evidence.json")
            if saved is not None:
                return saved
        audio, offset = self.audio_samples()
        if not whole:
            start = float(start if start is not None else max(0, offset))
            end = float(end if end is not None else offset + len(audio) / 16000)
            if not all(math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= self.info()["duration_seconds"] or end - start > 30:
                raise ValueError("局部声音复核范围须为原视频秒数，0≤start<end≤时长，每次最多30秒")
            begin = max(0, int((start - offset) * 16000))
            finish = max(begin, min(len(audio), int((end - offset) * 16000)))
            result = analyze(audio[begin:finish], offset=offset + begin / 16000, start=start, end=end)
            if self.asr_available() and finish > begin:
                result["local_transcription"] = self._decode_audio(audio[begin:finish], offset + begin / 16000)
                result["transcription_note"] = "所选范围重新转写，边界可能截词；与原全文核对，不按声学候选自动分配说话人。"
        else:
            result = analyze(audio, offset=offset)
            self._write_json("voice_evidence.json", result)
        return result

    def _decode_audio(self, samples, offset=0.0):
        import sherpa_onnx
        if not self.asr_available():
            raise ValueError("本地SenseVoice模型不可用")
        if self._recognizer is None:
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self.model_dir / "model.int8.onnx"), tokens=str(self.model_dir / "tokens.txt"),
                num_threads=4, language="zh", use_itn=True)
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self._recognizer.decode_stream(stream)
        return {"text": stream.result.text.strip(), "tokens": list(stream.result.tokens),
                "timestamps": [round(t + offset, 3) for t in stream.result.timestamps]}

    def transcribe(self):
        if "transcript" in self._cache:
            return self._cache["transcript"]
        saved = self._read_json("transcript.json")
        if saved is not None:
            self._cache["transcript"] = saved
            return saved
        import numpy as np
        started = time.monotonic()
        audio, offset = self.audio_samples()
        if not len(audio):
            result = {"has_audio": False, "segments": [], "full_text": "", "note": "视频中没有可解码音轨"}
            self._write_json("transcript.json", result)
            self._cache["transcript"] = result
            return result
        if not self.asr_available():
            raise ValueError("缺少本地SenseVoice模型；需配置含model.int8.onnx和tokens.txt的模型目录，本工具不自动下载")
        # Whole-file decode preserves the sentence sequence. Segments below provide
        # coarse audio windows, not word-level alignment or a substitute for listening.
        def decode(samples):
            return self._decode_audio(samples)
        full = decode(audio)
        full_text = full["text"]
        hop = 320
        rms = np.sqrt(np.mean(np.pad(audio, (0, (-len(audio)) % hop)).reshape(-1, hop) ** 2, axis=1))
        quiet = max(0.003, min(float(np.percentile(rms, 20)) * 1.3, 0.02))
        boundaries = [0]
        duration = len(audio) / 16000
        while duration - boundaries[-1] > 10:
            begin = int((boundaries[-1] + 5) / 0.02)
            end = min(len(rms), int((boundaries[-1] + 10) / 0.02))
            # Prefer sustained pauses over a hard fixed-width cut.
            smooth = np.convolve(rms[begin:end], np.ones(8) / 8, mode="valid")
            candidates = np.flatnonzero(smooth < quiet)
            position = int(candidates[len(candidates) // 2]) if len(candidates) else int(np.argmin(smooth))
            boundaries.append(round((begin + position + 4) * 0.02, 3))
        boundaries.append(duration)
        segments = []
        for start, end in zip(boundaries, boundaries[1:]):
            part = decode(audio[int(start * 16000):int(end * 16000)])
            part["timestamps"] = [round(t + start + offset, 3) for t in part["timestamps"]]
            segments.append({"start": round(start + offset, 3), "end": round(end + offset, 3), **part})
        result = {"has_audio": True, "method": "本地SenseVoiceSmall INT8，中文自动转写",
                  "full_text": full_text, "segments": segments, "seconds": round(time.monotonic() - started, 3),
                  "tokens": full["tokens"], "timestamps": [round(t + offset, 3) for t in full["timestamps"]],
                  "timing_accuracy": "timestamps为模型估计的token起始时间（原视频秒数），不是强制对齐；分段为约5–10秒窗口，边界可能截词，以完整转写核对句序。",
                  "warning": "自动转写不是人工听核；数字品牌否定词须复核，不能据此判断真实声线、情绪或配乐。"}
        self._cache["transcript"] = result
        self._write_json("transcript.json", result)
        return result

def text_result(value):
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}


def definitions(allow_segments=True):
    try:
        from .video_segments import SEGMENT_TOOL
    except ImportError:
        from video_segments import SEGMENT_TOOL
    def tool(name, description, properties=None, required=None):
        return {"name": name, "description": description, "inputSchema": {
            "type": "object", "properties": properties or {}, "required": required or [], "additionalProperties": False}}
    return ([SEGMENT_TOOL] if allow_segments else []) + [
        tool("inspect_video", "检查本次原视频时长、画幅、帧率、音轨和本地转写模型是否可用。"),
        tool("detect_shots", "逐帧检测切镜候选与前后区间；候选须补帧核对，快速运动可能误报。",
             {"threshold": {"type": "number", "minimum": 0.05, "maximum": 0.5}}),
        tool("get_frames", "按秒从原视频补抽1–12帧并直接返回带时间标签的图片，用于检查切点、动作和页面细节。",
             {"times": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"type": "number", "minimum": 0}}}, ["times"]),
        tool("transcribe_audio", "读取原视频音轨并进行本地中文自动转写；返回全文和粗略音频窗口，不能用于判断声线或音乐。"),
        tool("analyze_voices", "对原音轨做分时段音高及音色变化复核；返回换声候选而非确定人数/性别。可指定疑点start/end秒数，最多30秒；省略则读取全片证据。",
             {"start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0}}),
        tool("diarize_speakers", "对原音轨做本地说话人分段与声纹聚类，返回估计人数、每人发言时间、音高及频谱音色指标。判断人数或多声线时优先调用。CPU按需加载；不转写、不推断性别。标签仅在本次录音有效。",
             {"num_speakers": {"type": "integer", "minimum": 0, "maximum": 20, "default": 0,
                               "description": "0为自动估计；仅已知人数时指定正整数"},
              "threshold": {"type": "number", "minimum": .1, "maximum": 1.5, "default": .5,
                            "description": "自动聚类阈值；越小通常分出越多人，调整后须复核"}}),
    ]
