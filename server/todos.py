"""Aggregated "what do I still need to do" view (/v1/todos). Read-only; reuses claude_panel / tasks_store readers.

GET /v1/todos -> {generated_at, groups: [{source, items}], summary: {total_open, total_done_today}}
  voice_trigger   OpenSpec changes written by a voice-triggered Claude run (claude_runs.jsonl) with unchecked tasks
  voice_synth     tasks.json v2 items with status open|reminded (done / dismissed / claude are not "to do")
  other_openspec  every other OpenSpec change with unchecked tasks
Groups with no items are omitted; each group is most-recent-activity first. Any unreadable item is skipped.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends

import claude_panel as CP
import tasks_store as S

RUNS_SCANNED = 40                      # newest voice runs considered (same window as /v1/claude/runs)
SKIP_DIRS = {"archive", "archives", "_archive"}
_SKIP = (OSError, ValueError, KeyError, TypeError, AttributeError)
_log_cache: dict[tuple, str | None] = {}   # (log_path, mtime_ns, size) -> change id mentioned by that run's log


def _is_today(ts: float) -> bool:
    return ts > 0 and date.fromtimestamp(ts) == date.today()


def _change_dirs(root: Path, ws: Path) -> dict[str, Path]:
    """change id -> dir: ws/openspec/changes/* first (what ChangeDetail resolves), then paths task_regroup recorded."""
    found: dict[str, Path] = {}

    def add(d: Path) -> None:
        try:
            d = d.resolve()
            ok = d.is_dir() and d.is_relative_to(ws) and d.parent.name == "changes" and d.parent.parent.name == "openspec"
        except OSError:
            return
        if ok and d.name not in SKIP_DIRS and not d.name.startswith("."):
            found.setdefault(d.name, d)

    try:
        for d in sorted((ws / "openspec/changes").iterdir()):
            add(d)
    except OSError:
        pass
    try:
        for g in json.loads(CP.tree_file(root).read_text(encoding="utf-8")):
            for c in g.get("changes", []):
                if c.get("path"):
                    add(ws / c["path"])
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return found


def _read_change(ws: Path, d: Path) -> dict:
    """title + done/total from proposal.md / tasks.md, and last activity (dir or tasks.md mtime, whichever is newer)."""
    prop = (d / "proposal.md").read_text(encoding="utf-8", errors="ignore") if (d / "proposal.md").exists() else ""
    tp = d / "tasks.md"
    items = CP.parse_checklist(tp.read_text(encoding="utf-8", errors="ignore")) if tp.exists() else []
    title = next((re.sub(r"^#+\s*(proposal:\s*)?", "", l, flags=re.I).strip() for l in prop.splitlines() if l.startswith("#")), "")
    done, total = sum(i["done"] for i in items), len(items)
    mt = max(d.stat().st_mtime, tp.stat().st_mtime if tp.exists() else 0.0)
    return {"id": d.name, "title": title or d.name, "done": done, "total": total, "mtime": mt,
            "percent": 100 * done // total if total else 0, "change_path": str(d.relative_to(ws))}


def _run_change(r: dict, ws: Path) -> str | None:
    lp = r.get("log_path") or ""
    try:
        st = Path(lp).stat()
    except OSError:
        return None
    key = (lp, st.st_mtime_ns, st.st_size)
    if key not in _log_cache:
        _log_cache[key] = CP._proposal(lp, ws)[0]
    return _log_cache[key]


def _voice_runs(root: Path, ws: Path) -> dict[str, dict]:
    """change id -> newest voice run that wrote its proposal."""
    import claude_tasks as T
    out: dict[str, dict] = {}
    try:
        runs = [r for r in T.read_runs(root) if r.get("kind") != "skipped"]
    except OSError:
        return out
    for r in sorted(runs, key=lambda r: CP._ts(r.get("started")), reverse=True)[:RUNS_SCANNED]:
        try:
            cid = _run_change(r, ws)
            if cid:
                out.setdefault(cid, {"run_name": f"aha-{r['recording_id']}", "started": r.get("started")})
        except _SKIP:
            continue
    return out


def _voice_tasks(root: Path) -> tuple[list[dict], int]:
    """(open/reminded rows newest-updated first, number of tasks marked done today)."""
    doc = S.load_doc(Path(root) / "inbox" / "tasks.json")
    if doc.get("version") != 2:          # v1 is migrated by GET /v1/tasks; nothing to curate here
        return [], 0
    rows, done_today = [], 0
    for it in doc["items"]:
        try:
            st = it.get("status", "open")
            upd = CP._ts(it.get("updated") or it.get("created"))
            if st == "done":
                done_today += _is_today(upd)
            elif st in ("open", "reminded"):
                row = {"kind": "voice_task", "id": it["id"], "title": it["title"], "next": it.get("next") or "",
                       "area": it.get("area"), "topic": it.get("topic"), "status": st, "source": "voice_synth",
                       "created": it.get("created"), "_t": upd}
                if it.get("change_id"):
                    row["change_id"] = it["change_id"]
                rows.append(row)
        except _SKIP:
            continue
    rows.sort(key=lambda r: -r["_t"])
    return rows, done_today


def build_todos(root: Path) -> dict:
    root = Path(root)
    ws = CP.workspace_path().resolve()
    linked = _voice_runs(root, ws)
    trigger, other, done_today = [], [], 0
    for cid, d in _change_dirs(root, ws).items():
        try:
            c = _read_change(ws, d)
        except _SKIP:
            continue
        if c["total"] and c["done"] == c["total"]:
            done_today += _is_today(c["mtime"])      # fully checked off, tasks.md touched today
            continue
        if not c["total"]:
            continue                                  # no tasks.md / no checklist: nothing to track
        row = {"kind": "openspec", "id": c["id"], "title": c["title"], "done": c["done"], "total": c["total"],
               "percent": c["percent"], "change_path": c["change_path"], "_t": c["mtime"]}
        run = linked.get(cid)
        if run:
            trigger.append({**row, "source": "voice_trigger", **run, "_t": CP._ts(run["started"])})
        else:
            other.append({**row, "source": "other", "source_hint": "manual"})
    synth, synth_done = _voice_tasks(root)
    groups = []
    for source, items in (("voice_trigger", trigger), ("voice_synth", synth), ("other_openspec", other)):
        items.sort(key=lambda r: -r["_t"])
        if items:
            groups.append({"source": source, "items": [{k: v for k, v in r.items() if k != "_t"} for r in items]})
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "groups": groups,
            "summary": {"total_open": sum(len(g["items"]) for g in groups), "total_done_today": done_today + synth_done}}


def router(root: Path, auth) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @r.get("/todos")
    def todos():
        return build_todos(root)

    return r
