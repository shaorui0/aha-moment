"""Experiment: on-device (iPhone, WhisperKit) vs Mac transcription bench.

GET  /v1/asr/prompt  -> the Mac's Whisper initial prompt (so the phone can use the identical text)
POST /v1/bench/asr   -> append one result record to <data>/bench/ios_asr.jsonl
"""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

ASR_DIR = Path(__file__).resolve().parent.parent / "asr"
FALLBACK_PROMPT = ("以下是简体中文的口语记录，夹杂英文技术词，例如 Kubernetes、Doris、FE、VictoriaMetrics、P99、Agent、"
                   "oncall、OpenSpec、Claude Code、Sonnet、DeepSeek、SQLite、Tailscale、CloudCLI、Aha、session、kubectl。")


def mac_prompt() -> str:
    """Same text mlx_whisper_backend uses (env AHA_WHISPER_PROMPT honoured); light import, no mlx needed."""
    try:
        if str(ASR_DIR) not in sys.path:
            sys.path.insert(0, str(ASR_DIR))
        from aha_asr.backends import mlx_whisper_backend
        return mlx_whisper_backend.PROMPT
    except Exception:  # noqa
        return FALLBACK_PROMPT


def router(root: Path, auth) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])
    f = Path(root) / "bench" / "ios_asr.jsonl"

    @r.get("/asr/prompt")
    def prompt():
        return {"prompt": mac_prompt()}

    @r.post("/bench/asr")
    def bench_asr(body: dict):
        rid = body.get("id")
        if not isinstance(rid, str) or not rid or not isinstance(body.get("text"), str):
            raise HTTPException(400, 'body must be an object with string "id" and "text"')
        rec = {**body, "received_at": time.time()}
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"ok": True, "id": rid}

    return r
