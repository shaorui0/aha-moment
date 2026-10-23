"""Audio helpers: ffmpeg decode to 16k mono, VAD gate."""
import os
import shutil
import subprocess
import tempfile
import wave

import numpy as np

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
SR = 16000


def to_wav16k(path: str, out_dir: str) -> str:
    out = os.path.join(out_dir, "in16k.wav")
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", path, "-ac", "1",
                    "-ar", str(SR), "-c:a", "pcm_s16le", out], check=True)
    return out


def load_pcm(path: str) -> np.ndarray:
    """float32 mono 16k samples in [-1,1]."""
    with tempfile.TemporaryDirectory() as d:
        w = to_wav16k(path, d)
        with wave.open(w, "rb") as f:
            raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def duration_s(path: str) -> float:
    return len(load_pcm(path)) / SR


# VAD gate (env-overridable): need >= MIN_SPEECH_S of speech AND speech/total >= MIN_SPEECH_RATIO.
MIN_SPEECH_S = float(os.environ.get("AHA_VAD_MIN_SPEECH_S", "1.0"))
MIN_SPEECH_RATIO = float(os.environ.get("AHA_VAD_MIN_SPEECH_RATIO", "0.08"))


def passes_gate(pcm: np.ndarray, ranges) -> bool:
    total = len(pcm) / SR
    sp = sum(b - a for a, b in ranges)
    return sp >= MIN_SPEECH_S and total > 0 and sp / total >= MIN_SPEECH_RATIO


_vad_path = None
SILERO_URL = "https://github.com/snakers4/silero-vad/raw/v5.1.2/src/silero_vad/data/silero_vad.onnx"


def _silero_file():
    """silero_vad.onnx (~2MB), cached under ~/.cache/aha_asr. None if unobtainable."""
    global _vad_path
    if _vad_path:
        return _vad_path
    d = os.path.expanduser("~/.cache/aha_asr")
    p = os.path.join(d, "silero_vad.onnx")
    if not os.path.exists(p):
        import urllib.request
        os.makedirs(d, exist_ok=True)
        with urllib.request.urlopen(SILERO_URL, timeout=60) as r, open(p + ".tmp", "wb") as f:
            f.write(r.read())
        os.replace(p + ".tmp", p)
    _vad_path = p
    return p


def speech_ranges(pcm: np.ndarray) -> list[tuple[float, float]]:
    """Speech intervals (s). silero-vad (via sherpa-onnx, no torch) if available, else energy fallback."""
    try:
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = _silero_file()
        cfg.silero_vad.min_silence_duration = 0.4
        cfg.silero_vad.min_speech_duration = 0.2
        cfg.sample_rate = SR
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
        out = []
        win = 512
        for i in range(0, len(pcm) - win + 1, win):
            vad.accept_waveform(pcm[i:i + win])
            while not vad.empty():
                seg = vad.front
                out.append((seg.start / SR, (seg.start + len(seg.samples)) / SR))
                vad.pop()
        vad.flush()
        while not vad.empty():
            seg = vad.front
            out.append((seg.start / SR, (seg.start + len(seg.samples)) / SR))
            vad.pop()
        return out
    except Exception:
        return _energy_ranges(pcm)


def _energy_ranges(pcm: np.ndarray, frame=0.03, thresh_db=-45.0):
    n = int(frame * SR)
    if len(pcm) < n:
        return []
    k = len(pcm) // n
    fr = pcm[: k * n].reshape(k, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    on = db > thresh_db
    out, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        if not v and start is not None:
            if (i - start) * frame >= 0.2:
                out.append((start * frame, i * frame))
            start = None
    if start is not None and (k - start) * frame >= 0.2:
        out.append((start * frame, k * frame))
    return out
