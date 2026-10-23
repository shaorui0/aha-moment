"""Shared helpers for the Aha pipeline (stdlib only, read-only on item dirs)."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

GOOD_STATUS = {"done", None}          # None: meta without status but with transcript
DEFAULT_SLICE_MS = 60_000


def data_root() -> Path:
    return Path(os.environ.get("AHA_DATA_DIR") or "~/aha_data").expanduser()


def claude_bin() -> str | None:
    """AHA_CLAUDE_BIN wins; otherwise ~/.local/bin (native install) is searched BEFORE PATH. launchd's PATH for
    com.aha.server lacked it, so which() picked the old brew claude 2.1.119, which has no --permission-prompts."""
    import shutil
    env = os.environ.get("AHA_CLAUDE_BIN")
    if env:
        return env
    return shutil.which("claude", path=os.pathsep.join([str(Path.home() / ".local" / "bin"), os.environ.get("PATH", "")]))


def write_atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def run_claude(prompt: str, timeout: int = 180) -> str:
    """`claude -p` with NO tools (voice text is untrusted input). Raises on failure."""
    exe = claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = [exe, "-p", "--tools", "", "--no-session-persistence"]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr.strip()[:500]}")
    out = r.stdout.strip()
    if not out:
        raise RuntimeError("claude returned empty output")
    return out


# ---------------------------------------------------------------- items
@dataclass
class Item:
    id: str
    source: str
    start: datetime          # aware, local tz
    duration_ms: int | None
    text: str
    dir: Path
    priority: bool = False   # uploaded via the Recordings cloud button / quick (server meta "priority")
    session_id: str | None = None      # one phone recording (start->stop); None for chunks from older app builds
    session_start: datetime | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(milliseconds=self.duration_ms or DEFAULT_SLICE_MS)


@dataclass
class Block:
    source: str
    start: datetime
    end: datetime
    items: list[Item] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(i.text for i in self.items)


def _transcript_text(d: Path, meta: dict) -> str | None:
    slugs = [t.get("slug") for t in (meta.get("transcripts") or []) if t.get("slug")]
    for s in reversed(slugs):   # newest transcript wins (reprocess appends), same rule as server/storage.py
        p = d / f"transcript.{s}.txt"
        if p.exists():
            return p.read_text(encoding="utf-8")
    for p in sorted(d.glob("transcript.*.txt")):   # tolerant fallback
        return p.read_text(encoding="utf-8")
    return None


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (t if t.tzinfo else t.astimezone()).astimezone()
    except ValueError:
        return None


def load_items(root: Path, day: str) -> tuple[list[Item], dict]:
    """Items recorded on `day` (YYYY-MM-DD) with usable transcripts + skip stats."""
    datetime.strptime(day, "%Y-%m-%d")  # validate
    ddir = root / day.replace("-", "/")
    items, stats = [], {"total": 0, "silent": 0, "failed": 0, "pending": 0, "empty": 0, "bad": 0}
    for mp in sorted(ddir.glob("*/meta.json")):
        stats["total"] += 1
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            st = meta.get("transcription_status")
            if st == "silent":
                stats["silent"] += 1; continue
            if st == "failed":
                stats["failed"] += 1; continue
            if st not in GOOD_STATUS:
                stats["pending"] += 1; continue
            text = _transcript_text(mp.parent, meta)
            if text is None or not text.strip():
                stats["empty"] += 1; continue
            start = datetime.fromisoformat(meta["recorded_at"].replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.astimezone()
            items.append(Item(meta["id"], meta.get("source") or "ambient", start.astimezone(),
                              meta.get("duration_ms"), text.strip(), mp.parent, bool(meta.get("priority")),
                              meta.get("session_id"), _parse_ts(meta.get("session_start"))))
        except Exception:
            stats["bad"] += 1
    items.sort(key=lambda i: i.start)
    return items, stats


def build_blocks(items: list[Item], gap_s: int = 120, max_block_min: int = 30) -> list[Block]:
    """Merge consecutive ambient items (gap <= gap_s, block <= max_block_min). Quick = own block."""
    blocks: list[Block] = []
    for it in items:
        b = blocks[-1] if blocks else None
        if (b and it.source == "ambient" and b.source == "ambient"
                and (it.start - b.end).total_seconds() <= gap_s
                and (it.end - b.start).total_seconds() <= max_block_min * 60):
            b.items.append(it)
            b.end = max(b.end, it.end)
        else:
            blocks.append(Block(it.source, it.start, it.end, [it]))
    return blocks
