"""Filesystem layout + meta persistence for Aha Moment.

Layout: <root>/YYYY/MM/DD/<id>/{audio.<ext>, meta.json, transcript.<slug>.txt|json}
Everything is written via temp-file + os.replace so readers never see partial files.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SOURCES = ("ambient", "quick")
EXT_BY_TYPE = {
    "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/mp4": "m4a", "audio/aac": "aac", "audio/x-aac": "aac", "audio/aacp": "aac",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
    "audio/wave": "wav", "audio/x-caf": "caf", "audio/caf": "caf", "audio/ogg": "ogg",
    "audio/webm": "webm", "audio/flac": "flac",
}


def data_root() -> Path:
    return Path(os.environ.get("AHA_DATA_DIR") or "~/aha_data").expanduser()


def token_file() -> Path:
    return Path(os.environ.get("AHA_TOKEN_FILE") or "~/.aha/token").expanduser()


def load_token(announce=print) -> str:
    """AHA_TOKEN env > token file; generate (0600) on first run."""
    env = os.environ.get("AHA_TOKEN")
    if env:
        return env.strip()
    f = token_file()
    if f.exists():
        return f.read_text().strip()
    f.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    announce(f"[aha] generated new token at {f}")
    return tok


def ext_for(content_type: str | None) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    return EXT_BY_TYPE.get(ct, "m4a")


def parse_tags(raw: str | None) -> list[str]:
    """X-Aha-Tags: comma-separated, trimmed/lowercased, deduped, order kept. Empty/absent -> []."""
    out: list[str] = []
    for t in (raw or "").split(","):
        t = t.strip().lower()
        if t and re.fullmatch(r"[a-z0-9_.:-]{1,32}", t) and t not in out:
            out.append(t)
    return out


def parse_ts(s: str | None) -> datetime:
    """ISO8601 -> aware local-tz datetime. None -> now. Raises ValueError if bad."""
    if not s:
        return datetime.now().astimezone()
    s = s.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> assume local
    return dt.astimezone()


def slugify(model: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", model or "unknown").strip("._-")
    return s or "unknown"


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def write_json_atomic(path: Path, obj) -> None:
    write_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2).encode())


class Store:
    """Item store with an in-memory meta index (rebuilt from disk at startup)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".tmp").mkdir(exist_ok=True)
        self.clean_tmp()
        self.lock = threading.RLock()
        self.items: dict[str, dict] = {}
        self.scan()

    # -- index ---------------------------------------------------------
    def scan(self) -> None:
        with self.lock:
            self.items.clear()
            for mp in self.root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*/meta.json"):
                try:
                    m = json.loads(mp.read_text())
                    self.items[m["id"]] = m
                except Exception:
                    continue

    def clean_tmp(self, max_age_s: float = 3600) -> int:
        """Delete spool files in .tmp older than max_age_s (leftovers of crashed/aborted uploads)."""
        cutoff, n = time.time() - max_age_s, 0
        for f in (self.root / ".tmp").iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(); n += 1
            except OSError:
                continue
        return n

    def tmp_path(self) -> Path:
        return self.root / ".tmp" / f"up-{secrets.token_hex(8)}"

    def dir_of(self, m: dict) -> Path:
        return self.root / m["dir"]

    def audio_path(self, m: dict) -> Path:
        return self.dir_of(m) / m["audio"]

    def get(self, item_id: str) -> dict | None:
        with self.lock:
            m = self.items.get(item_id)
            return json.loads(json.dumps(m)) if m else None

    # -- mutation ------------------------------------------------------
    def commit_upload(self, item_id: str, tmp: Path, sha: str, *, recorded_at: datetime,
                      source: str, ext: str, duration_ms: int | None, size: int,
                      priority: bool = False, session: dict | None = None,
                      tags: list[str] | None = None):
        """Returns ('created'|'exists'|'conflict', meta). A re-PUT with priority upgrades an existing item."""
        with self.lock:
            cur = self.items.get(item_id)
            if cur:
                os.unlink(tmp)
                if cur["sha256"] != sha:
                    return "conflict", cur
                if priority and not cur.get("priority"):
                    cur["priority"] = True
                    write_json_atomic(self.dir_of(cur) / "meta.json", cur)
                return "exists", json.loads(json.dumps(cur))
            rel = f"{recorded_at:%Y/%m/%d}/{item_id}"
            d = self.root / rel
            d.mkdir(parents=True, exist_ok=True)
            audio = f"audio.{ext}"
            os.replace(tmp, d / audio)  # atomic: tmp lives in same filesystem
            m = {
                "id": item_id, "source": source, "recorded_at": recorded_at.isoformat(),
                "received_at": datetime.now().astimezone().isoformat(),
                "sha256": sha, "size": size, "duration_ms": duration_ms,
                "dir": rel, "audio": audio,
                "transcription_status": "pending", "attempts": 0, "lifetime_attempts": 0, "error": None,
                "transcripts": [], "dispatched": False,
            }
            if priority:
                m["priority"] = True
            if tags:
                m["tags"] = tags
            if session:   # one phone recording (start->stop) = one session; chunks share session_id
                m.update(session)
            write_json_atomic(d / "meta.json", m)
            self.items[item_id] = m
            return "created", json.loads(json.dumps(m))

    def update(self, item_id: str, **fields) -> dict:
        with self.lock:
            m = self.items[item_id]
            m.update(fields)
            write_json_atomic(self.dir_of(m) / "meta.json", m)
            return json.loads(json.dumps(m))

    def add_session_tags(self, session_id: str, tags: list[str]) -> list[str]:
        """Merge tags into every meta.json of a session (quick items: session id == item id). Returns touched item ids."""
        touched: list[str] = []
        with self.lock:
            for m in self.items.values():
                if (m.get("session_id") or m["id"]) != session_id:
                    continue
                merged = list(m.get("tags") or [])
                merged += [t for t in tags if t not in merged]
                if merged != (m.get("tags") or []):
                    m["tags"] = merged
                    write_json_atomic(self.dir_of(m) / "meta.json", m)
                touched.append(m["id"])
        return touched

    def add_transcript(self, item_id: str, result: dict) -> tuple[dict, bool]:
        """Write transcript.<slug>.txt/json without ever overwriting. Returns (meta, written)."""
        with self.lock:
            m = self.items[item_id]
            slug = slugify(result.get("model") or result.get("backend") or "unknown")
            d = self.dir_of(m)
            txt, js = d / f"transcript.{slug}.txt", d / f"transcript.{slug}.json"
            if txt.exists() or js.exists():
                return json.loads(json.dumps(m)), False
            write_atomic(txt, (result.get("text") or "").encode())
            write_json_atomic(js, result)
            m["transcripts"].append({"slug": slug, "backend": result.get("backend"),
                                     "model": result.get("model"),
                                     "created_at": datetime.now().astimezone().isoformat()})
            write_json_atomic(d / "meta.json", m)
            return json.loads(json.dumps(m)), True

    def transcript_text(self, m: dict, slug: str | None = None) -> str | None:
        ts = m.get("transcripts") or []
        if not ts:
            return None
        slug = slug or ts[-1]["slug"]   # newest transcript wins (reprocess appends)
        p = self.dir_of(m) / f"transcript.{slug}.txt"
        return p.read_text() if p.exists() else None

    # -- queries -------------------------------------------------------
    def pending_ids(self) -> list[str]:
        with self.lock:
            xs = [m for m in self.items.values()
                  if m["transcription_status"] in ("pending", "processing", "failed")]
        return [m["id"] for m in sorted(xs, key=lambda m: m["recorded_at"])]

    def latest_priority(self) -> dict | None:
        with self.lock:
            xs = [m for m in self.items.values() if m.get("priority") and m.get("priority_done_at")]
            m = max(xs, key=lambda m: m["priority_done_at"], default=None)
            return json.loads(json.dumps(m)) if m else None

    def counts(self) -> dict:
        c: dict[str, int] = {}
        with self.lock:
            for m in self.items.values():
                c[m["transcription_status"]] = c.get(m["transcription_status"], 0) + 1
        return c

    def recent(self, n=20) -> list[dict]:
        with self.lock:
            xs = sorted(self.items.values(), key=lambda m: m["received_at"], reverse=True)[:n]
        return [{k: m.get(k) for k in ("id", "source", "recorded_at", "received_at",
                                       "transcription_status", "attempts", "lifetime_attempts", "size")} for m in xs]

    def on_date(self, day: str) -> list[dict]:
        """day = YYYY-MM-DD; items sorted by recorded_at."""
        pre = day.replace("-", "/") + "/"
        with self.lock:
            xs = [m for m in self.items.values() if m["dir"].startswith(pre)]
        return sorted(xs, key=lambda m: m["recorded_at"])
