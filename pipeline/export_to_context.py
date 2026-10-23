#!/usr/bin/env python3
"""One recording -> one text file in the context-infrastructure daily log.

<workspace>/contexts/daily_records/YYYY-MM-DD_HHMM_aha.md   (HHMM = recording start; "-2", "-3" on same-minute clashes)

A recording is one start->stop on the phone (chunks share session_id). Chunks from older app builds without a
session are grouped by contiguity (quick = its own recording). The file holds only the transcript text, chunks in
order; it is rewritten while the recording grows. Recordings with no text get no file. Files of this pattern that
no longer match a recording of that day are removed (they are ours: derived, regenerable from ~/aha_data).
Dry-run by default; --apply writes. AHA_CONTEXT_ROOT overrides the workspace root.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aha_common as C  # noqa: E402

DEFAULT_WS = Path(__file__).resolve().parents[3]  # pipeline -> aha_moment -> projects -> workspace
OURS = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_aha(-\d+)?\.md$")


def recordings(root: Path, day: str) -> list[tuple[datetime, str]]:
    """(start, text) for every recording that started on `day`, in start order."""
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    items = C.load_items(root, day)[0] + C.load_items(root, nxt)[0]   # a recording started on `day` may run past midnight
    by_session: dict[str, list[C.Item]] = {}
    legacy: list[C.Item] = []
    for it in items:
        (by_session.setdefault(it.session_id, []) if it.session_id else legacy).append(it)
    recs = []
    for its in by_session.values():
        its.sort(key=lambda i: i.start)
        start = its[0].session_start or its[0].start
        recs.append((start, its))
    for b in C.build_blocks(sorted(legacy, key=lambda i: i.start)):
        recs.append((b.start, b.items))
    out = []
    for start, its in sorted(recs, key=lambda r: r[0]):
        if start.strftime("%Y-%m-%d") != day:
            continue
        text = "\n".join(i.text.strip() for i in its if i.text.strip())
        if text:
            out.append((start, text))
    return out


def plan(day: str, recs: list[tuple[datetime, str]]) -> dict[str, str]:
    files: dict[str, str] = {}
    for start, text in recs:
        base = f"{day}_{start:%H%M}_aha"
        name, n = f"{base}.md", 1
        while name in files:
            n += 1
            name = f"{base}-{n}.md"
        files[name] = text + "\n"
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    try:
        datetime.strptime(a.date, "%Y-%m-%d")
    except ValueError:
        print("bad --date", file=sys.stderr)
        return 2
    ws = Path(os.environ.get("AHA_CONTEXT_ROOT") or DEFAULT_WS)
    ddir = ws / "contexts" / "daily_records"
    files = plan(a.date, recordings(C.data_root(), a.date))
    stale = [p for p in (ddir.glob(f"{a.date}_*_aha*.md") if ddir.exists() else [])
             if OURS.match(p.name) and not p.name.endswith(".enhance.md") and p.name not in files]
    changed = [n for n, t in files.items() if not (ddir / n).exists() or (ddir / n).read_text(encoding="utf-8") != t]
    print(f"{a.date}: {len(files)} recordings, {len(changed)} new/changed, {len(stale)} stale -> {ddir}")
    if not a.apply:
        print("dry-run (use --apply to write)")
        return 0
    for n in changed:
        C.write_atomic_text(ddir / n, files[n])
    for p in stale:
        p.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
