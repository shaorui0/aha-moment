"""SenseVoiceSmall via sherpa-onnx (int8, CPU, no torch needed)."""
import importlib.util
import re

REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
_TAG = re.compile(r"<\|[^|]*\|>")


def available() -> bool:
    return importlib.util.find_spec("sherpa_onnx") is not None


class Backend:
    model_name = "SenseVoiceSmall-int8 (sherpa-onnx)"

    def __init__(self):
        import sherpa_onnx
        from huggingface_hub import hf_hub_download
        model = hf_hub_download(REPO, "model.int8.onnx")
        tokens = hf_hub_download(REPO, "tokens.txt")
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, num_threads=4, use_itn=True, language="auto")
        self._lang_cache = {}

    def transcribe(self, wav_path: str, language=None) -> dict:
        import wave
        import numpy as np
        with wave.open(wav_path, "rb") as f:
            sr = f.getframerate()
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        dur = len(pcm) / sr
        # sherpa SenseVoice is trained on short utterances: chunk at 30s.
        step = 30 * sr
        segs, lang = [], None
        for i in range(0, len(pcm), step):
            s = self._rec.create_stream()
            s.accept_waveform(sr, pcm[i:i + step])
            self._rec.decode_stream(s)
            r = s.result
            text = _TAG.sub("", r.text).strip()
            lang = lang or (r.lang.strip("<|>") if getattr(r, "lang", "") else None)
            if text:
                segs.append({"start": i / sr, "end": min((i + step) / sr, dur), "text": text})
        return {"text": "".join(x["text"] for x in segs), "language": lang, "segments": segs}
