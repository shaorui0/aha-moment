"""Qwen3-ASR via mlx-audio (MLX, Apple Silicon)."""
import importlib.util

REPOS = {"0.6B": "mlx-community/Qwen3-ASR-0.6B-8bit",
         "1.7B": "mlx-community/Qwen3-ASR-1.7B-8bit"}


def available() -> bool:
    return importlib.util.find_spec("mlx_audio") is not None


class Backend:
    def __init__(self, size="0.6B"):
        from mlx_audio.stt.utils import load_model
        self.model_name = REPOS[size]
        self._m = load_model(self.model_name)

    def transcribe(self, wav_path: str, language=None) -> dict:
        kw = {}
        if language:
            kw["language"] = {"zh": "Chinese", "en": "English", "ja": "Japanese"}.get(language, language)
        r = self._m.generate(wav_path, **kw)
        text = getattr(r, "text", str(r)).strip()
        segs = []
        for s in (getattr(r, "segments", None) or []):
            segs.append({"start": float(s.get("start", 0)), "end": float(s.get("end", 0)),
                         "text": s.get("text", "")})
        if not segs and text:
            segs = [{"start": 0.0, "end": 0.0, "text": text}]
        return {"text": text, "language": getattr(r, "language", None) or language, "segments": segs}
