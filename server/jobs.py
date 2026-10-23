"""Background-job panel (/v1/jobs): single read-only snapshot of what the Aha pipeline is doing right now.

Sources (each one tolerant of missing files):
  dispatch        tail of ~/Library/Logs/aha-dispatch.log (last "claude tasks started: N" line + mtime)
  enhance         /tmp/aha_correct_classify.log tail + enhanced vs raw count for today
  synthesize      <data>/inbox/tasks_synth.json (same shape /v1/tasks uses)
  digest          periodic_jobs/nightly_digest/out/ newest file + mtime (optional)
  claude_runs     last 10 lines of <data>/inbox/claude_runs.jsonl
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, date
from pathlib import Path

from fastapi import APIRouter, Depends


def _tail(path: Path, n: int = 20) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            chunk = min(size, 8192)
            fh.seek(size - chunk)
            data = fh.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return []
    return [ln for ln in data.splitlines()[-n:] if ln.strip()]


def _mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        return None


def _dispatch() -> dict:
    log = Path.home() / "Library" / "Logs" / "aha-dispatch.log"
    tail = _tail(log, 60)
    last_tick = next((l for l in reversed(tail) if "claude tasks started" in l), None)
    m = re.search(r"started:\s*(\d+)", last_tick or "")
    return {"last_log_mtime": _mtime(log), "last_tick_line": last_tick, "last_tick_started": int(m.group(1)) if m else None}


def _enhance(records_dir: Path) -> dict:
    log = Path("/tmp/aha_correct_classify.log")
    tail = _tail(log, 20)
    today = date.today().isoformat()
    raw = [p.name for p in records_dir.glob(f"{today}_*_aha*.md") if "enhance" not in p.name]
    enh = [p.name for p in (records_dir / "enhanced").glob(f"{today}_*_aha*.enhance.md")] \
        if (records_dir / "enhanced").is_dir() else []
    return {"log_mtime": _mtime(log), "tail": tail[-5:], "today_raw": len(raw), "today_enhanced": len(enh),
            "pending": max(0, len(raw) - len(enh))}


def _synth(root: Path) -> dict:
    p = root / "inbox" / "tasks_synth.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: d.get(k) for k in ("state", "started", "finished", "last_ok", "error", "stats")}


def _digest() -> dict:
    out = Path(__file__).resolve().parents[2].parent / "periodic_jobs" / "nightly_digest" / "out"
    if not out.is_dir():
        return {}
    newest = max(out.glob("*"), key=lambda p: p.stat().st_mtime, default=None)
    return {"last_output": newest.name if newest else None, "last_mtime": _mtime(newest) if newest else None}


def _claude_runs(root: Path) -> list[dict]:
    p = root / "inbox" / "claude_runs.jsonl"
    out = []
    for line in _tail(p, 10):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def router(root: Path, records_dir: Path, auth) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @r.get("/jobs")
    def jobs():
        return {
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "dispatch": _dispatch(),
            "enhance": _enhance(Path(records_dir)),
            "synthesize_tasks": _synth(Path(root)),
            "digest": _digest(),
            "claude_runs": _claude_runs(Path(root)),
        }

    return r
