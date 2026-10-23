import importlib.util

import os

REPO = "mlx-community/whisper-large-v3-turbo"
# Biases output toward Simplified Chinese and keeps tech terms in English; override via env.
PROMPT = os.environ.get("AHA_WHISPER_PROMPT",
                        "以下是简体中文的口语记录，夹杂英文技术词，例如 Kubernetes、Doris、FE、VictoriaMetrics、P99、Agent、"
                        "oncall、OpenSpec、Claude Code、Sonnet、DeepSeek、SQLite、Tailscale、CloudCLI、Aha、session、kubectl。")


# Per-segment drop thresholds (env-overridable). Note mlx_whisper reports these per 30s decode
# window, so repeated-loop hallucinations share one (high) compression_ratio.
NSP_THRESH = float(os.environ.get("AHA_WHISPER_NSP", "0.6"))
NSP_LOGPROB = float(os.environ.get("AHA_WHISPER_NSP_LOGPROB", "-0.7"))
MIN_LOGPROB = float(os.environ.get("AHA_WHISPER_MIN_LOGPROB", "-1.0"))
MAX_COMPRESSION = float(os.environ.get("AHA_WHISPER_MAX_COMPRESSION", "2.4"))


def keep_segment(s: dict) -> bool:
    nsp = s.get("no_speech_prob", 0.0)
    lp = s.get("avg_logprob", 0.0)
    cr = s.get("compression_ratio", 0.0)
    if nsp > NSP_THRESH and lp < NSP_LOGPROB:
        return False
    return not (lp < MIN_LOGPROB or cr > MAX_COMPRESSION)


def available() -> bool:
    return importlib.util.find_spec("mlx_whisper") is not None


class Backend:
    # "+f2" = filtering version (VAD gate + segment drop); bump when filtering changes so reprocess writes a new
    # transcript file instead of being skipped by the never-overwrite rule.
    model_name = REPO + "+f2"

    def __init__(self):
        import mlx_whisper  # noqa
        self._mw = mlx_whisper
        self._loaded = False

    def transcribe(self, wav_path: str, language=None) -> dict:
        kw = dict(path_or_hf_repo=REPO, condition_on_previous_text=False,
                  no_speech_threshold=0.6, compression_ratio_threshold=2.4,
                  verbose=None, initial_prompt=PROMPT or None)
        if language:
            kw["language"] = language
        r = self._mw.transcribe(wav_path, **kw)
        raw = r.get("segments", [])
        segs = [{"start": float(s["start"]), "end": float(s["end"]), "text": s["text"]}
                for s in raw if keep_segment(s)]
        text = "".join(s["text"] for s in segs) if len(segs) != len(raw) else r.get("text", "")
        return {"text": text, "language": r.get("language"), "segments": segs}
