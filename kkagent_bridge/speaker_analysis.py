"""Lazy, recording-local speaker diarization; no ASR or language model.

The ONNX models run on CPU. Acoustic measurements describe the
recorded signal, not gender, personality, or an enrolled person's identity.
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import time

import numpy as np

try:
    from .voice_analysis import acoustic_track, summarize
except ImportError:  # MCP stdio entry point
    from voice_analysis import acoustic_track, summarize

SCHEMA_VERSION = 1


def model_directory():
    configured = os.environ.get("KKGO_SPEAKER_MODEL_DIR", "").strip()
    return (Path(configured).expanduser() if configured else
            Path(__file__).resolve().parents[1] / "models" / "speaker-diarization")


def settings():
    return {"model_dir": model_directory(), "provider": "cpu"}


def availability():
    config = settings()
    directory = config["model_dir"]
    return {"models_available": all((directory / name).is_file() for name in
                                    ("segmentation.onnx", "speaker_embedding.onnx")),
            "provider_requested": config["provider"], "loaded": False,
            "note": "CPU按需加载；不需要显卡运行库"}


def validate_options(num_speakers, threshold):
    if isinstance(num_speakers, bool) or not isinstance(num_speakers, int) or not 0 <= num_speakers <= 20:
        raise ValueError("num_speakers 须为0（自动估计）或1–20的整数")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not .1 <= threshold <= 1.5:
        raise ValueError("threshold 须为0.1–1.5的有限数；越小通常分出越多说话人")


class SpeakerAnalyzer:
    def __init__(self, directory, provider="cpu"):
        if provider != "cpu":
            raise ValueError("说话人分析仅使用CPU后端")
        self.directory = Path(directory)
        self.provider = provider
        self._engine = None
        self._options = None

    def fingerprint(self):
        # Invalidate job-local reports when the configuration or weights change.
        items = [SCHEMA_VERSION, self.provider, str(self.directory.resolve())]
        for name in ("segmentation.onnx", "speaker_embedding.onnx"):
            path = self.directory / name
            stat = path.stat() if path.is_file() else None
            items.append([name, stat.st_size, stat.st_mtime_ns] if stat else [name, None])
        return hashlib.sha256(repr(items).encode()).hexdigest()[:20]

    def _load(self, num_speakers, threshold):
        options = (num_speakers, threshold, self.fingerprint())
        if self._engine is not None and self._options == options:
            return self._engine
        for name in ("segmentation.onnx", "speaker_embedding.onnx"):
            if not (self.directory / name).is_file():
                raise ValueError("缺少说话人模型；请重新解压包含 models 的完整包，"
                                 "或设置 KKGO_SPEAKER_MODEL_DIR。本工具不在任务中下载模型。")
        import sherpa_onnx
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(self.directory / "segmentation.onnx")),
                provider=self.provider, num_threads=2),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self.directory / "speaker_embedding.onnx"),
                provider=self.provider, num_threads=2),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_speakers if num_speakers else -1, threshold=threshold),
            min_duration_on=.3, min_duration_off=.5)
        if not config.validate():
            raise ValueError("说话人模型配置无效")
        # Drop the previous sessions before constructing sessions with new options.
        self._engine = None
        self._engine = sherpa_onnx.OfflineSpeakerDiarization(config)
        self._options = options
        return self._engine

    def analyze(self, samples, *, offset=0., num_speakers=0, threshold=.5):
        validate_options(num_speakers, threshold)
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or not np.isfinite(samples).all():
            raise ValueError("需要有限值的16kHz单声道音频")
        started = time.monotonic()
        duration = len(samples) / 16000
        report = {"schema_version": SCHEMA_VERSION, "status": "no_audio",
                  "speaker_count": None, "speakers": [], "segments": [],
                  "range": {"start": round(offset, 3), "end": round(offset + duration, 3)},
                  "provider_requested": self.provider, "num_speakers_requested": num_speakers,
                  "threshold": threshold,
                  "method": "sherpa-onnx: pyannote segmentation 3.0 + 中文CAM++声纹聚类；NumPy声学统计",
                  "limitations": "人数是模型估计；标签仅在本次录音内有效。抢话、配乐、变声和极短发言可能误分。"
                                 "声学指标受录音设备影响，不据此确定性别、年龄或温柔/磁性等主观风格。"}
        if not len(samples):
            return report
        engine = self._load(num_speakers, threshold)
        detected = engine.process(samples).sort_by_start_time()
        intervals = []
        for segment in detected:
            a, b = max(0., float(segment.start)), min(duration, float(segment.end))
            if math.isfinite(a) and math.isfinite(b) and b > a:
                intervals.append((int(segment.speaker), a, b))
        # Stable, human-readable IDs follow first appearance, not native cluster IDs.
        ids = {native: f"speaker_{index}" for index, native in enumerate(dict.fromkeys(s[0] for s in intervals))}
        segments = [{"speaker_id": ids[native], "start": round(a + offset, 3), "end": round(b + offset, 3)}
                    for native, a, b in intervals]
        rows = acoustic_track(samples, offset=offset) if intervals else []
        times = np.array([row["time"] for row in rows])
        speakers = []
        for native, speaker_id in ids.items():
            own = [(a, b) for who, a, b in intervals if who == native]
            # Exclude overlap from timbre measurements; mixed speech is not one voice.
            selected = []
            for a, b in own:
                left, right = np.searchsorted(times, [a + offset, b + offset])
                selected.extend(row for row in rows[left:right] if not any(
                    who != native and c + offset <= row["time"] < d + offset for who, c, d in intervals))
            metrics = summarize(selected, offset, offset + duration)
            for key in ("start", "end"):
                metrics.pop(key)
            voiced = [row for row in selected if row["pitch"] is not None]
            # Additional descriptors are physical statistics, not guessed style labels.
            metrics["spectral_centroid_median_hz"] = (
                round(float(np.median([row["spectral_centroid_hz"] for row in voiced])), 1) if voiced else None)
            metrics["high_frequency_energy_ratio_median"] = (
                round(float(np.median([row["high_frequency_energy_ratio"] for row in voiced])), 3) if voiced else None)
            pitch = metrics["pitch_median_hz"]
            description = (f"可测有声音段基频中位数约{pitch}Hz，"
                           f"10–90%范围{metrics['pitch_p10_p90_hz']}Hz。" if pitch is not None else "有效周期性音段不足，无法稳定描述音高。")
            a, b = max(own, key=lambda pair: pair[1] - pair[0])
            speakers.append({"id": speaker_id,
                             "speech_seconds": round(sum(b - a for a, b in own), 3),
                             "segment_count": len(own), "acoustic_features": metrics,
                             "voice_description": description,
                             "representative_range": {"start": round(a + offset, 3),
                                                      "end": round(min(b, a + 5) + offset, 3)}})
        report.update(status="analyzed" if segments else "no_speech_detected",
                      speaker_count=len(speakers), speakers=speakers, segments=segments,
                      seconds=round(time.monotonic() - started, 3),
                      speaker_count_status="自动聚类估计" if not num_speakers else "按调用者指定人数聚类，非独立人数验证")
        return report
