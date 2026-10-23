#!/usr/bin/env python3
"""Tail ~/aha_data/inbox/quick.jsonl and route each utterance per routes.json.

Cursor (~/aha_data/inbox/.cursor) = byte offset of the first unprocessed line, advanced after
each line. Side effects are idempotent (id marker in target file / answers/<id>.md exists), so a
crash between effect and cursor update does not duplicate output. Partial trailing line is left
for the next run. flock prevents overlapping runs.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aha_common as C  # noqa: E402
import claude_tasks as CT  # noqa: E402

ROUTES = Path(__file__).resolve().parent / "routes.json"
SEP = " \t,:，：、。.;；"


def load_routes(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def match(text: str, routes: dict) -> tuple[dict, str]:
    """Return (rule, body)."""
    t = text.strip()
    low = t.lower()
    for rule in routes["rules"]:
        for p in rule.get("prefixes", []):
            pl = p.lower()
            if low.startswith(pl):
                rest = t[len(p):]
                if p[-1].isascii() and p[-1].isalnum() and rest[:1].isascii() and rest[:1].isalnum():
                    continue  # "asked", "todolist": not a command
                return rule, rest.lstrip(SEP).strip()
        for k in rule.get("keywords", []):
            if k.lower() in low:
                return rule, t
    return routes["default"], t


def append_once(path: Path, item_id: str, line: str) -> bool:
    """Append line unless an entry carrying this id is already there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and item_id in path.read_text(encoding="utf-8"):
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")
        fh.flush()
    return True


def fmt_time(rec: dict) -> str:
    try:
        dt = datetime.fromisoformat(str(rec.get("recorded_at")).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def handle(rec: dict, routes: dict, inbox: Path, dry: bool, log) -> str:
    rid = re.sub(r"[^A-Za-z0-9_-]", "_", str(rec["id"]))
    if CT.TAG in (rec.get("tags") or []):   # tagged `claude`: owned by claude_tasks.scan (reads meta.json), never routed here
        return f"{rid}: tagged '{CT.TAG}' -> handled by claude_tasks"
    rule, body = match(str(rec.get("text") or ""), routes)
    name = rule.get("name", "default")
    if not body:
        rule, name = routes["default"], "default(empty-body)"
        body = str(rec.get("text") or "").strip() or "(empty)"
    fields = dict(body=body, id=rid, time=fmt_time(rec), text=str(rec.get("text") or ""))
    if rule["action"] == "ask":
        ans = inbox / "answers" / f"{rid}.md"
        if dry:
            return f"{rid}: [{name}] ask claude -> {ans}  q={body!r}"
        if ans.exists():
            return f"{rid}: [{name}] answer exists, skip"
        try:
            a = C.run_claude(body)
            C.write_atomic_text(ans, f"# Q ({fmt_time(rec)})\n\n{body}\n\n# A\n\n{a}\n")
            return f"{rid}: [{name}] answered -> {ans}"
        except Exception as e:  # don't wedge the queue on one bad item
            d = routes["default"]
            append_once(inbox / d["target"], rid,
                        d["format"].format(**{**fields, "body": f"[ask failed: {e}] {body}"}))
            return f"{rid}: [{name}] FAILED ({e}); fell back to {d['target']}"
    target = inbox / rule["target"]
    line = rule["format"].format(**fields)
    if dry:
        return f"{rid}: [{name}] -> {target}: {line}"
    wrote = append_once(target, rid, line)
    return f"{rid}: [{name}] -> {target}" + ("" if wrote else " (already there)")


def run(inbox: Path, routes: dict, dry: bool = False, log=print) -> int:
    src, cur = inbox / "quick.jsonl", inbox / ".cursor"
    if not src.exists():
        return 0
    inbox.mkdir(parents=True, exist_ok=True)
    with open(inbox / ".cursor.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another dispatch run holds the lock; exiting")
            return 0
        try:
            off = int(cur.read_text().strip() or 0) if cur.exists() else 0
        except ValueError:
            off = 0
        size = src.stat().st_size
        if off > size:
            log(f"cursor {off} > file size {size}; file was truncated, restarting at 0")
            off = 0
        n = 0
        with open(src, "rb") as fh:
            fh.seek(off)
            while True:
                raw = fh.readline()
                if not raw or not raw.endswith(b"\n"):
                    break  # EOF or partial line still being written
                off_next = fh.tell()
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    try:
                        rec = json.loads(line)
                        if not isinstance(rec, dict) or "id" not in rec:
                            raise ValueError("no id")
                        log(handle(rec, routes, inbox, dry, log))
                        n += 1
                    except (ValueError, KeyError) as e:
                        log(f"bad line at {off}: {e}")
                        if not dry:
                            with open(inbox / "bad_lines.jsonl", "a", encoding="utf-8") as bf:
                                bf.write(line + "\n")
                if not dry:
                    C.write_atomic_text(cur, str(off_next))
                off = off_next
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show routing; no writes, no cursor move, no claude")
    ap.add_argument("--routes", type=Path, default=ROUTES)
    a = ap.parse_args(argv)
    n = run(C.data_root() / "inbox", load_routes(a.routes), a.dry_run)
    try:                                    # voice -> claude tasks (own lock; never wedges quick routing)
        k = CT.scan(dry=a.dry_run)
        print(f"claude tasks started: {k}" + (" [dry-run]" if a.dry_run else ""))
    except Exception as e:
        print(f"claude_tasks.scan failed: {e}")
    print(f"processed {n} utterance(s)" + (" [dry-run]" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
