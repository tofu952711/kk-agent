"""Local acoustic change evidence, not a speaker-count or gender classifier.

Mirrors the successful Codex pitch probe with time-local spectral evidence.
No learned speaker model, external API, SciPy dependency, or forced count.
"""
from __future__ import annotations
import math
import numpy as np


def acoustic_track(samples, sample_rate=16000, offset=0.0):
    samples = np.asarray(samples, dtype=np.float32)
    window, hop = 1024, 320
    if sample_rate != 16000:
        raise ValueError("声学分析需要16kHz单声道音频")
    rows = []
    for start in range(0, max(0, len(samples) - window + 1), hop):
        signal = samples[start:start + window].astype(np.float64)
        signal -= signal.mean()
        rms = float(np.sqrt(np.mean(signal ** 2)))
        signal *= np.hanning(window)
        spectrum = np.abs(np.fft.rfft(signal, n=2048)) ** 2
        frequencies = np.fft.rfftfreq(2048, 1 / sample_rate)
        total_power = float(spectrum.sum())
        centroid = float(np.dot(frequencies, spectrum) / total_power) if total_power > 1e-12 else 0.
        high_ratio = float(spectrum[frequencies >= 2000].sum() / total_power) if total_power > 1e-12 else 0.
        # Broad energy ratios provide a crude timbre cue, not identity.
        edges = (80, 250, 500, 1000, 2000, 4000, 7600)
        bands = np.array([spectrum[math.ceil(a * 2048 / sample_rate):math.ceil(b * 2048 / sample_rate)].sum()
                          for a, b in zip(edges, edges[1:])])
        envelope = np.log(bands + 1e-10)
        envelope -= envelope.mean()
        correlation = np.fft.irfft(spectrum, n=2048)[:window]
        low, high = int(sample_rate / 500), int(sample_rate / 70)
        peaks = np.flatnonzero((correlation[low+1:high] > correlation[low:high-1]) &
                              (correlation[low+1:high] >= correlation[low+2:high+1])) + low + 1
        pitch, periodicity = None, 0.0
        if len(peaks) and correlation[0] > 1e-12:
            lag = int(peaks[np.argmax(correlation[peaks])])
            periodicity = float(correlation[lag] / correlation[0])
            if periodicity >= .45:
                pitch = sample_rate / lag
        rows.append({"time": start / sample_rate + window / sample_rate / 2 + offset,
                     "rms": rms, "pitch": pitch, "periodicity": periodicity, "envelope": envelope,
                     "spectral_centroid_hz": centroid, "high_frequency_energy_ratio": high_ratio})
    if rows:
        gate = max(.003, float(np.percentile([r["rms"] for r in rows], 75)) * .10)
        for row in rows:
            if row["rms"] < gate:
                row["pitch"] = None
    return rows


def summarize(rows, start, end):
    selected = [r for r in rows if start <= r["time"] < end]
    voiced = [r for r in selected if r["pitch"] is not None]
    result = {"start": round(start, 3), "end": round(end, 3), "analyzed_windows": len(selected),
              "periodic_windows": len(voiced), "periodic_fraction": round(len(voiced) / max(1, len(selected)), 3),
              "pitch_median_hz": None, "pitch_p10_p90_hz": None}
    if voiced:
        pitches = [r["pitch"] for r in voiced]
        result.update(pitch_median_hz=round(float(np.median(pitches)), 1),
                      pitch_p10_p90_hz=[round(float(v), 1) for v in np.percentile(pitches, [10, 90])])
    return result


def analyze(samples, *, offset=0.0, start=None, end=None):
    """Rank sustained before/after changes; never equate a change to a person."""
    duration = len(samples) / 16000
    start = offset if start is None else start
    end = offset + duration if end is None else end
    rows = acoustic_track(samples, offset=offset)
    times = np.array([r["time"] for r in rows])
    def section(a, b):
        return rows[int(np.searchsorted(times, a)):int(np.searchsorted(times, b))]
    # Bound report size on long videos; no need to send thousands of rows.
    bin_seconds = max(1., (end - start) / 120)
    bins = [summarize(section(float(t), min(float(t) + bin_seconds, end)), float(t), min(float(t) + bin_seconds, end))
            for t in np.arange(start, end, bin_seconds)]
    candidates = []
    span = .65
    for boundary in np.arange(start + span, end - span, .1):
        left_rows = section(boundary - span, boundary - .05)
        right_rows = section(boundary + .05, boundary + span)
        left = [r for r in left_rows if r["pitch"] is not None]
        right = [r for r in right_rows if r["pitch"] is not None]
        if min(len(left), len(right)) < 9:
            continue
        lp, rp = (np.log2([r["pitch"] for r in group]) for group in (left, right))
        delta = abs(float(np.median(lp) - np.median(rp))) * 12
        spread = max(float(np.median(np.abs(lp - np.median(lp)))), float(np.median(np.abs(rp - np.median(rp))))) * 12
        spectral = float(np.sqrt(np.mean((np.median([r["envelope"] for r in left], axis=0) -
                                         np.median([r["envelope"] for r in right], axis=0)) ** 2)))
        if delta < 3.5 or delta < spread * 1.5 or spectral < .30:
            continue
        candidates.append({"time": round(float(boundary), 3), "pitch_shift_semitones": round(delta, 2),
                           "spectral_envelope_distance": round(spectral, 3),
                           "left": summarize(left_rows, boundary - span, boundary - .05),
                           "right": summarize(right_rows, boundary + .05, boundary + span),
                           "score": round(delta * min(2.0, spectral), 3),
                           "interpretation": "疑似声音变化；需结合口型、回应关系及前后声学段复核，非已确认换人"})
    chosen = []
    for candidate in sorted(candidates, key=lambda c: c["score"], reverse=True):
        if all(abs(candidate["time"] - c["time"]) >= 1.0 for c in chosen):
            chosen.append(candidate)
        if len(chosen) == 8:
            break
    chosen.sort(key=lambda c: c["time"])
    return {"status": "analyzed" if len(samples) else "no_audio", "method": "16kHz本地自相关音高+宽频带谱包络变化候选，非声纹分离模型",
            "speaker_count": None, "speaker_count_status": "未通过说话人分离模型核实，不能把候选数加1当作人数",
            "range": {"start": round(start, 3), "end": round(end, 3)}, "summary_window_seconds": bin_seconds,
            "windows": bins, "change_candidates": chosen,
            "limitations": "音高和谱包络变化可能来自同一人语调、变声、配乐或倍频/半频误差；不能据此确定性别、人数或身份。没有变化也不证明只有一人。",
            "visual_check": "对照变化前后可见口型、人物回应、跨镜接音；画外音是声音与画面的关系，同一主播也可画外持续说话。"}
