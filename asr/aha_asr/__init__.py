"""aha_asr: pluggable local ASR. Stable contract:

    list_backends() -> list[str]
    transcribe(audio_path, backend=None, language=None) -> dict
    has_speech(audio_path) -> bool
"""
import importlib
import os
import time

from . import guard

# name -> module (each exposes Backend with .model_name, .transcribe(wav_path, language))
_REGISTRY = {
    "mlx_whisper": "aha_asr.backends.mlx_whisper_backend",
    "sensevoice": "aha_asr.backends.sensevoice_backend",
    "qwen3_asr_0.6b": "aha_asr.backends.qwen3_asr_backend:0.6B",
    "qwen3_asr_1.7b": "aha_asr.backends.qwen3_asr_backend:1.7B",
}
_PREFERENCE = ["mlx_whisper", "sensevoice", "qwen3_asr_1.7b", "qwen3_asr_0.6b"]
_instances: dict = {}


def _available(name: str) -> bool:
    mod = _REGISTRY[name].split(":")[0]
    try:
        m = importlib.import_module(mod)
        return bool(m.available())
    except Exception:
        return False


def list_backends() -> list[str]:
    """Backends whose dependencies import (not necessarily downloaded)."""
    return [n for n in _REGISTRY if _available(n)]


def default_backend() -> str:
    env = os.environ.get("AHA_ASR_BACKEND")
    if env:
        return env
    avail = list_backends()
    for n in _PREFERENCE:
        if n in avail:
            return n
    raise RuntimeError("no ASR backend available")


def get_backend(name: str):
    if name not in _REGISTRY:
        raise ValueError(f"unknown backend {name!r}; known: {list(_REGISTRY)}")
    if name not in _instances:
        spec = _REGISTRY[name]
        mod, _, arg = spec.partition(":")
        m = importlib.import_module(mod)
        t0 = time.time()
        _instances[name] = m.Backend(arg) if arg else m.Backend()
        _instances[name].load_s = None  # filled by first transcribe
        _instances[name]._t_created = time.time() - t0
    return _instances[name]


def has_speech(audio_path: str) -> bool:
    from . import audio
    pcm = audio.load_pcm(audio_path)
    return audio.passes_gate(pcm, audio.speech_ranges(pcm))


def transcribe(audio_path: str, backend: str | None = None, language: str | None = None) -> dict:
    import tempfile
    from . import audio
    backend = backend or default_backend()
    t0 = time.time()
    pcm = audio.load_pcm(audio_path)
    dur = len(pcm) / audio.SR
    ranges = audio.speech_ranges(pcm)
    base = {"text": "", "language": language, "segments": [], "model": None,
            "backend": backend, "duration_s": dur, "elapsed_s": 0.0}
    be = get_backend(backend)
    base["model"] = be.model_name
    if not audio.passes_gate(pcm, ranges):
        base["elapsed_s"] = time.time() - t0
        return base
    with tempfile.TemporaryDirectory() as d:
        wav = audio.to_wav16k(audio_path, d)
        res = be.transcribe(wav, language)
    segs = guard.filter_segments(res["segments"], ranges)
    text = "".join(s["text"] for s in segs) if segs != res["segments"] else res["text"]
    if not segs and res["segments"]:
        text = ""
    base.update(text=text.strip(), language=res.get("language") or language, segments=segs)
    base["elapsed_s"] = time.time() - t0
    return base
