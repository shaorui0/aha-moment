"""Read-only nightly digest API (/v1/digest). Files are written by periodic_jobs/nightly_digest; this never generates."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

KINDS = ("aha", "sessions")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KEEP = 14


def digest_dir() -> Path:
    return Path(os.environ.get("AHA_DIGEST_DIR")
                or "~/work/context-infrastructure/periodic_jobs/nightly_digest/out").expanduser()


def available_dates(kind: str) -> list[str]:
    d = digest_dir() / kind
    return sorted((p.stem for p in d.glob("*.md") if DATE_RE.match(p.stem)), reverse=True)[:KEEP] if d.is_dir() else []


def router(auth) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @r.get("/digest")
    def digest(kind: str = "aha", date: str | None = None):
        if kind not in KINDS:
            raise HTTPException(400, f"kind must be one of {KINDS}")
        if date is not None and not DATE_RE.match(date):
            raise HTTPException(400, "date must be YYYY-MM-DD")
        dates = available_dates(kind)
        day = date or (dates[0] if dates else None)
        f = digest_dir() / kind / f"{day}.md" if day else None
        if f is None or not f.is_file():
            raise HTTPException(404, "no digest")
        return {"kind": kind, "date": day, "markdown": f.read_text(encoding="utf-8"),
                "generated_at": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                "available_dates": dates}

    return r
