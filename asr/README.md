# aha_asr — local ASR on Mac (M4 Pro / 24GB)

Contract: `list_backends()`, `transcribe(path, backend=None, language=None) -> dict`, `has_speech(path)`.
Default backend = `AHA_ASR_BACKEND` env, else first available: mlx_whisper → sensevoice → qwen3.

## Install (slow-network path)

Heavy wheels are fetched with parallel HTTP ranges (`ops/fastwheel.py`, `ops/bigwheels.py`) into
`~/.cache/aha_wheels`, then `pip install --no-index --no-deps`. Needed: mlx, mlx-metal, numpy, scipy,
numba, llvmlite, tiktoken, sherpa-onnx, **sherpa-onnx-core** (carries libonnxruntime), plus
psutil more-itertools tqdm huggingface_hub regex requests, and `mlx-whisper --no-deps` (skips torch).
Package is importable via `.venv/.../aha_asr_dev.pth` → `asr/`.

## Bench (2026-10-09, synthetic `say` samples — real voice eval still pending)

| backend | zh CER | mixed CER | ja CER | en CER | RTF (warm) | RSS |
|---|---|---|---|---|---|---|
| mlx_whisper large-v3-turbo + 简体 prompt | 0.00 | 0.06 | 0.03 | 0.00 | 0.14–0.20 | ~1.0 GB |
| sensevoice small int8 (sherpa-onnx) | 0.00 | 0.32 | 0.58 | 0.09 | 0.03–0.04 | ~0.7 GB |

- Silence (20s): both return empty; silero VAD gates ASR so silent segments never reach Whisper.
- Without the initial prompt Whisper emitted Traditional Chinese (們/後); `AHA_WHISPER_PROMPT` biases to 简体 + English tech terms.
- Known miss: "Grafana" → "Gryfana" (synthetic TTS voice; re-check on real recordings). Mixed sample re-recorded 2026-10-24.
- Throughput (e2e scenario 8): 60×60s backlog drained in 381s → RTF 0.106, ~9.5× realtime. 10h/day of audio ≈ 64 min of Mac compute.

**Default: mlx_whisper.** SenseVoice is 5× faster but mangles mixed zh/en tech terms and Japanese kana.

## Noise / hallucination filtering (env-overridable)

Layer 1, VAD gate (`audio.passes_gate`, used by `has_speech` and `transcribe`): total speech >= `AHA_VAD_MIN_SPEECH_S` (1.0) and speech/duration >= `AHA_VAD_MIN_SPEECH_RATIO` (0.08).
Layer 2, per-segment drop in the whisper backend: drop if (no_speech_prob > `AHA_WHISPER_NSP` 0.6 and avg_logprob < `AHA_WHISPER_NSP_LOGPROB` -0.7) or avg_logprob < `AHA_WHISPER_MIN_LOGPROB` (-1.0) or compression_ratio > `AHA_WHISPER_MAX_COMPRESSION` (2.4). mlx_whisper reports these per 30s window and no_speech_prob is usually 0 on noise, so compression_ratio (looping text) is the effective signal.
Layer 3, `guard.py`: stock phrases (incl. exact "thank you"/"谢谢") dropped when outside VAD speech.
Empty text is a valid result; audio is never touched.

## Not done
- Qwen3-ASR (0.6B/1.7B 8-bit weights are downloaded in HF cache) needs `mlx-audio`; backend code is untested.
- Real-voice eval: drop own recordings + `.ref.txt` into `samples/` and run `.venv/bin/python asr/bench.py`.
