"""Voice tasks (/v1/tasks): v2 items written by pipeline/synthesize_tasks.py into <data>/inbox/tasks.json.

GET  /v1/tasks[?undelivered=1]      v2 items (+ v1-compatible note/delivered) and synthesis status
POST /v1/tasks/ack {ids}            status open -> reminded      (Shortcut / app pushed them to Reminders)
POST /v1/tasks/dismiss {ids}        status open -> dismissed
POST /v1/tasks/done {ids}           status * -> done              (user explicitly marks complete)
POST /v1/tasks/resynthesize         202, detached synthesize_tasks.py (already_running guard)
POST /v1/tasks/{id}/to_claude       start a voice-style claude task from title + next + evidence, status -> claude
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid as uuidlib
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))
import tasks_store as S  # noqa: E402

_change_cache: dict = {"at": 0.0}


def _status(root: Path) -> dict:
    p = Path(root) / "inbox" / "tasks_synth.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    import claude_tasks as T
    if st.get("state") == "running" and not T.pid_alive(st.get("pid")):
        st["state"] = "error"; st["error"] = "synthesis process vanished"
    return {k: st.get(k) for k in ("state", "started", "finished", "last_ok", "error", "stats")}


def _resolve_changes(root: Path, f: Path) -> None:
    """A task handed to Claude gets change_id once its run log mentions openspec/changes/<id>/proposal.md that exists."""
    import claude_tasks as T
    from claude_panel import _proposal
    if time.time() - _change_cache["at"] < 10:
        return
    _change_cache["at"] = time.time()
    with S.locked(f):
        doc = S.load_doc(f)
        found = False
        for it in doc["items"]:
            if it.get("status") == "claude" and not it.get("change_id") and it.get("claude_uuid"):
                cid, _ = _proposal(str(T.runs_dir(Path(root)) / f"{it['claude_uuid']}.log"), Path(T.workspace()))
                if cid:
                    it["change_id"] = cid; found = True
        if found:
            S.save_doc(f, doc)


def claude_prompt(it: dict) -> str:
    return (f"任务：{it['title']}\n下一步：{it.get('next') or '（未写）'}\n\n来自语音的原话片段：\n"
            + "\n".join(f"- {e.get('quote', '')}" for e in it.get("evidence", [])[:8]))


def router(root: Path, auth) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])
    f = Path(root) / "inbox" / "tasks.json"

    def load() -> list[dict]:
        return S.load_doc(f)["items"]

    @r.get("/tasks")
    def tasks(undelivered: int = 0):
        if f.exists() and not S.is_v2(f):
            S.migrate_v1(f)
        try:
            _resolve_changes(Path(root), f)
        except Exception:  # noqa: BLE001 - display nicety only
            pass
        items = load()
        if undelivered:
            items = [i for i in items if i.get("status", "open") == "open"]
        return {"items": [S.public(i) for i in items], "synth": _status(root)}

    def mark(body, to: str) -> int:
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(400, 'body must be {"ids": ["..."]}')
        want, n = set(ids), 0
        with S.locked(f):
            doc = S.load_doc(f)
            for i in doc["items"]:
                if i.get("id") in want and i.get("status", "open") == "open":
                    i["status"] = to
                    i["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    n += 1
            if n:
                S.save_doc(f, doc)
        return n

    @r.post("/tasks/ack")
    def ack(body: dict):
        return {"acked": mark(body, "reminded")}

    @r.post("/tasks/dismiss")
    def dismiss(body: dict):
        return {"dismissed": mark(body, "dismissed")}

    @r.post("/tasks/done")
    def done(body: dict):
        """Explicit user "mark complete". Unlike ack/dismiss, allowed from ANY non-done status."""
        ids = body.get("ids") if isinstance(body, dict) else None
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(400, 'body must be {"ids": ["..."]}')
        want, n = set(ids), 0
        with S.locked(f):
            doc = S.load_doc(f)
            for i in doc["items"]:
                if i.get("id") in want and i.get("status") != "done":
                    i["status"] = "done"
                    i["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    n += 1
            if n:
                S.save_doc(f, doc)
        return {"done": n}

    @r.post("/tasks/resynthesize", status_code=202)
    def resynthesize():
        if _status(root)["state"] == "running":
            return {"status": "already_running"}
        p = subprocess.Popen([sys.executable, str(PIPELINE / "synthesize_tasks.py")], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True, env={**os.environ, "AHA_DATA_DIR": str(root)})
        st = _raw_status(root)
        st.update(state="running", pid=p.pid, started=datetime.now().astimezone().isoformat(timespec="seconds"))
        (Path(root) / "inbox").mkdir(parents=True, exist_ok=True)
        (Path(root) / "inbox" / "tasks_synth.json").write_text(json.dumps(st, ensure_ascii=False))
        return {"status": "started"}

    def _raw_status(root) -> dict:
        try:
            return json.loads((Path(root) / "inbox" / "tasks_synth.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @r.post("/tasks/{tid}/to_claude")
    def to_claude(tid: str):
        import claude_tasks as T
        running = [x for x in T.read_runs(Path(root)) if x.get("status") == "running" and T.pid_alive(x.get("pid"))]
        if len(running) >= T.MAX_RUNNING:
            raise HTTPException(409, f"{len(running)} claude runs active, try later")
        with S.locked(f):
            doc = S.load_doc(f)
            it = next((i for i in doc["items"] if i.get("id") == tid), None)
            if it is None:
                raise HTTPException(404, "no such task")
            if it.get("status") == "claude":
                raise HTTPException(409, "already handed to claude")
            u = str(uuidlib.uuid4())
            try:
                T.launch(Path(root), "new", u, f"task-{tid}", T.PROMPT.format(text=claude_prompt(it)))
            except RuntimeError as e:
                raise HTTPException(500, str(e))
            it["status"], it["claude_uuid"] = "claude", u
            it["updated"] = datetime.now().astimezone().isoformat(timespec="seconds")
            S.save_doc(f, doc)
        return {"status": "started", "uuid": u, "name": f"aha-task-{tid}"}

    return r
