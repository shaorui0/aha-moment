"""tasks.json v2 helpers shared by pipeline/synthesize_tasks.py, pipeline/extract_tasks.py and server/tasks.py.

v2 document: {"version": 2, "items": [{id, title, next, area, topic, evidence:[{source, para, quote}],
status: open|reminded|claude|dismissed, change_id?, claude_uuid?, created, updated}], "legacy": [...]}.
v1 (no "version" key): flat segment items with delivered/dismissed/stale flags; migrate_v1() converts it once
and keeps the original next to it as tasks.v1.json.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

STATUSES = ("open", "reminded", "claude", "dismissed", "done")
AREAS = ("工作", "个人")


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".tasks.lock", "w") as fh:   # same lock as the v1 code
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def load_doc(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 2, "items": []}
    if not isinstance(d, dict) or not isinstance(d.get("items"), list):
        return {"version": 2, "items": []}
    d.setdefault("version", 1)
    return d


def save_doc(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def is_v2(path: Path) -> bool:
    return load_doc(path).get("version") == 2 and path.exists()


def make_note(it: dict) -> str:
    """Text pushed into Reminders / handed to Claude: next step + evidence quotes."""
    parts = [it.get("next") or ""]
    quotes = [f"· {e.get('quote', '')}" for e in it.get("evidence", []) if e.get("quote")]
    if quotes:
        parts.append("\n".join(quotes[:6]))
    return "\n\n".join(p for p in parts if p)


def public(it: dict) -> dict:
    """What GET /v1/tasks returns: the v2 item plus v1-compatible `note` / `delivered` (Shortcut, old app)."""
    out = dict(it)
    out["note"] = make_note(it)
    out["delivered"] = it.get("status", "open") != "open"
    out["done"] = it.get("status") == "done"
    return out


def migrate_v1(path: Path) -> bool:
    """v1 -> v2 in place (idempotent). Delivered/dismissed v1 items become v2 items with that status; the other
    (open/stale) v1 segments are kept under `legacy` as evidence for the first synthesis. Returns True if migrated."""
    with locked(path):
        doc = load_doc(path)
        if doc.get("version") == 2 and path.exists():
            return False
        old = doc["items"]
        bak = path.with_name("tasks.v1.json")
        if path.exists() and not bak.exists():
            shutil.copy2(path, bak)
        items, legacy = [], []
        for i in old:
            quote = (i.get("note") or i.get("title") or "").split("\n\n— ", 1)[0]
            if i.get("dismissed") or i.get("delivered"):
                items.append({"id": i["id"], "title": i.get("title", ""), "next": "", "area": "个人", "topic": "其他",
                              "evidence": [{"source": i.get("source", ""), "para": None, "quote": quote[:80]}],
                              "status": "dismissed" if i.get("dismissed") else "reminded",
                              "created": i.get("created", ""), "updated": i.get("created", "")})
            elif not i.get("stale"):
                legacy.append({"id": i["id"], "source": i.get("source", ""), "created": i.get("created", ""), "text": quote})
        save_doc(path, {"version": 2, "items": items, "legacy": legacy})
        return True
