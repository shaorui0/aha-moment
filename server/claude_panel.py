"""Read-only Claude panel API for the iOS app (/v1/claude/*). Sessions/usage come from the agent-harness
tracker (read-only, never spawns claude); runs/stop reuse pipeline/claude_tasks.py; tasks tree is a JSON
file written by pipeline/task_regroup.py."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

BOARD_TTL_S = 15
WS_NAME = "context-infrastructure"
STATE = {"running": "running", "needs_input": "needs_input", "done": "done", "stale": "stale", "exited": "done"}
_board_cache: dict = {"at": 0.0, "data": None}


def tracker_dir() -> Path:
    return Path(os.environ.get("AHA_TRACKER_DIR") or "~/work/context-infrastructure/projects/agent-harness/tracker").expanduser()


def usage_file() -> Path:
    return Path(os.environ.get("AHA_TRACKER_USAGE") or "~/.harness/tracker/state/usage.json").expanduser()


def fetch_board() -> dict:
    """`tracker/show.py --json` (read-only collector), cached BOARD_TTL_S."""
    if _board_cache["data"] is not None and time.time() - _board_cache["at"] < BOARD_TTL_S:
        return _board_cache["data"]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE", "CLAUDECODE"))}
    r = subprocess.run([sys.executable, str(tracker_dir() / "show.py"), "--json"], capture_output=True, text=True,
                       timeout=60, cwd=str(tracker_dir()), env=env)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:] or f"tracker exit {r.returncode}")
    data = json.loads(r.stdout)
    _board_cache.update(at=time.time(), data=data)
    return data


def read_usage() -> dict:
    try:
        d = json.loads(usage_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"ok": False}
    except (OSError, ValueError):
        return {"ok": False, "error": "no usage cache"}


def _ts(s) -> float:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _cut(s, n=200):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


_TITLE_RE = re.compile(rb'"(customTitle|aiTitle)":"((?:[^"\\]|\\.){1,200})"')
_title_cache: dict[str, tuple[float, str | None]] = {}


def session_title(sid: str) -> str | None:
    """Claude Code 自己记的标题：/rename 的 customTitle 优先，其次自动生成的 aiTitle（各取最后一次）。"""
    f = claude_projects_dir() / f"{sid}.jsonl"
    try:
        mt = f.stat().st_mtime
    except OSError:
        return None
    hit = _title_cache.get(sid)
    if hit and hit[0] == mt:
        return hit[1]
    last = {}
    try:
        for kind, val in _TITLE_RE.findall(f.read_bytes()):
            last[kind] = val
    except OSError:
        return None
    raw = last.get(b"customTitle") or last.get(b"aiTitle")
    title = json.loads(b'"' + raw + b'"') if raw else None
    _title_cache[sid] = (mt, title)
    return title


def _session_row(s: dict, changes: dict) -> dict:
    cids = s.get("change_ids") or []
    items, prog = [], None
    for cid in cids:
        c = changes.get(cid)
        if c:
            items += [{"text": _cut(it["text"].split(" | ")[0], 140), "done": bool(it.get("done")),
                       "current": bool(it.get("current"))} for it in c.get("items", [])]
            p = c.get("progress") or {}
            if p.get("total"):
                prog = {"done": (prog or {}).get("done", 0) + p["done"], "total": (prog or {}).get("total", 0) + p["total"]}
    if not items:
        items = [{"text": _cut(t.get("text") or t.get("content") or "", 140), "done": bool(t.get("done")),
                  "current": bool(t.get("current"))} for t in s.get("todo_items") or []]
        if s.get("todo_total"):
            prog = {"done": s.get("todo_done", 0), "total": s["todo_total"]}
    return ({
        "sid": s["session_id"],
        "title": session_title(s["session_id"]) or s.get("name") or s.get("project_name") or s["session_id"][:8],
        "state": STATE.get(s.get("phase"), "done"), "waiting_for": s.get("waiting_for"),
        "since": s.get("phase_since"), "updated": s.get("status_updated_at"),
        "started": s.get("started_at"), "model": s.get("model"),
        "progress": prog, "changes": cids, "tasks": items[:60],
        "last_user": _cut(s.get("last_human_prompt")),
        "last_assistant": _cut(s.get("last_text") or s.get("last_action")),
    })


def build_sessions(board: dict, usage: dict, now: float | None = None) -> dict:
    now = now or time.time()
    win_start = None
    for w in usage.get("windows") or []:
        if w.get("id") == "five_hour" and w.get("resets_at"):
            win_start = w["resets_at"] - 5 * 3600
    midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    since = min(win_start, midnight) if win_start else midnight
    ws = next((w for w in board.get("workspaces", []) if w.get("name") == WS_NAME), None) or {}
    changes = {c["id"]: c for c in ws.get("changes", [])}
    out = []
    for s in ws.get("sessions", []):
        upd = _ts(s.get("status_updated_at") or s.get("phase_since"))
        if upd < since:
            continue
        out.append(_session_row(s, changes))
    order = {"running": 0, "needs_input": 1, "stale": 2, "done": 3}
    out.sort(key=lambda x: (order[x["state"]], -_ts(x["updated"])))
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "workspace": WS_NAME,
            "window_start": datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds"), "sessions": out[:80]}


PROPOSAL_RE = re.compile(r"openspec/changes/([A-Za-z0-9._-]+)/proposal\.md")


def _first_line(store, rid: str) -> str:
    metas = [m for m in store.items.values() if m.get("session_id") == rid or m.get("id") == rid]
    for m in sorted(metas, key=lambda m: m.get("recorded_at") or ""):
        t = (store.transcript_text(m) or "").strip()
        if t:
            return _cut(t.splitlines()[0], 80)
    return ""


def _proposal(log_path: str, ws: Path) -> tuple[str | None, str | None]:
    try:
        txt = Path(log_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    for cid in reversed(PROPOSAL_RE.findall(txt)):
        if (ws / "openspec/changes" / cid / "proposal.md").exists():
            return cid, f"openspec/changes/{cid}/proposal.md"
    return None, None


def build_runs(store, root: Path) -> dict:
    import claude_tasks as T
    ws = Path(T.workspace())
    runs = []
    for r in reversed(T.read_runs(root)):
        if r.get("kind") == "skipped":
            continue
        running = r.get("status") == "running" and T.pid_alive(r.get("pid"))
        status = r.get("status") if (r.get("status") != "running" or running) else "lost"
        cid, path = _proposal(r.get("log_path") or "", ws)
        runs.append({"uuid": r["uuid"], "name": f"aha-{r['recording_id']}", "recording_id": r["recording_id"],
                     "kind": r.get("kind"), "status": status, "started": r.get("started"), "finished": r.get("finished"),
                     "title": _first_line(store, r["recording_id"]), "change_id": cid, "proposal_path": path})
    return {"runs": runs[:40]}


def tree_file(root: Path) -> Path:
    return root / "claude_tasks_tree.json"


def status_file(root: Path) -> Path:
    return root / "claude_tasks_regroup.json"


def build_tasks(root: Path) -> dict:
    tf, sf = tree_file(root), status_file(root)
    try:
        groups = json.loads(tf.read_text(encoding="utf-8"))
        gen = datetime.fromtimestamp(tf.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except (OSError, ValueError):
        groups, gen = [], None
    try:
        st = json.loads(sf.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    import claude_tasks as T
    if st.get("state") == "running" and not T.pid_alive(st.get("pid")):
        st["state"] = "error"; st["error"] = "regroup process vanished"
    return {"generated_at": gen, "groups": groups, "regroup": {k: st.get(k) for k in ("state", "started", "finished", "error")}}


# ------------------------------------------------------------------ detail endpoints (read-only)
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RUN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,36}$")
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
BOX_RE = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*\S)\s*$")
PROPOSAL_MAX = 8000


def workspace_path() -> Path:
    import claude_tasks as T
    return Path(T.workspace())


def claude_projects_dir() -> Path:
    if os.environ.get("AHA_CLAUDE_PROJECTS"):
        return Path(os.environ["AHA_CLAUDE_PROJECTS"]).expanduser()
    return Path("~/.claude/projects").expanduser() / re.sub(r"[^A-Za-z0-9]", "-", str(workspace_path()))


def _msg_text(msg) -> str:
    c = (msg or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def read_transcript_tail(f: Path, n: int = 10, cut: int = 500) -> list[dict]:
    msgs: list[dict] = []
    try:
        size = f.stat().st_size
        with f.open("rb") as fh:
            fh.seek(max(0, size - 4_000_000))
            raw = fh.read().decode("utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    for line in raw:
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if not isinstance(o, dict) or o.get("type") not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
            continue
        t = _msg_text(o.get("message")).strip()
        if not t or t.startswith("<"):  # tool results / system-injected wrappers
            continue
        msgs.append({"role": o["type"], "text": _cut(t, cut), "at": o.get("timestamp")})
    return msgs[-n:]


def parse_checklist(md: str) -> list[dict]:
    out = []
    for line in md.splitlines():
        m = BOX_RE.match(line)
        if m:
            out.append({"text": _cut(m.group(2), 200), "done": m.group(1) != " "})
    return out


def _change_dir(root: Path, cid: str) -> Path | None:
    """ws/openspec/changes/<cid>, or the nested path recorded by task_regroup; always confined to the workspace."""
    ws = workspace_path().resolve()
    cands = [ws / "openspec/changes" / cid]
    try:
        for g in json.loads(tree_file(root).read_text(encoding="utf-8")):
            for c in g.get("changes", []):
                if c.get("id") == cid and c.get("path"):
                    cands.append(ws / c["path"])
    except (OSError, ValueError, AttributeError):
        pass
    for d in cands:
        try:
            d = d.resolve()
        except OSError:
            continue
        if d.is_dir() and d.name == cid and d.is_relative_to(ws) and d.parent.name == "changes" and d.parent.parent.name == "openspec":
            return d
    return None


def build_change(root: Path, cid: str) -> dict | None:
    d = _change_dir(root, cid)
    if d is None:
        return None

    def rd(n):
        try:
            return (d / n).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    prop, tasks = rd("proposal.md"), rd("tasks.md")
    title = next((re.sub(r"^#+\s*(proposal:\s*)?", "", l, flags=re.I).strip() for l in prop.splitlines() if l.startswith("#")), cid)
    items = parse_checklist(tasks)
    return {"id": cid, "title": title or cid, "proposal": prop[:PROPOSAL_MAX], "truncated": len(prop) > PROPOSAL_MAX,
            "tasks": items, "done": sum(i["done"] for i in items), "total": len(items)}


def build_session_detail(sid: str, board: dict | None) -> dict | None:
    f = claude_projects_dir() / f"{sid}.jsonl"
    meta = None
    changes: dict = {}
    if board:
        ws = next((w for w in board.get("workspaces", []) if w.get("name") == WS_NAME), None) or {}
        changes = {c["id"]: c for c in ws.get("changes", [])}
        raw = next((s for s in ws.get("sessions", []) if s.get("session_id") == sid), None)
        meta = _session_row(raw, changes) if raw else None
    if meta is None and not f.is_file():
        return None
    d = dict(meta or {"sid": sid, "title": sid[:8], "state": "done", "tasks": [], "changes": []})
    d["title"] = session_title(sid) or d.get("title")
    d["workspace"] = str(workspace_path())
    d["messages"] = read_transcript_tail(f) if f.is_file() else []
    d["change_links"] = [{"id": c, "title": (changes.get(c) or {}).get("title") or c} for c in d.get("changes") or []]
    return d


def build_run_detail(store, root: Path, uuid: str) -> dict | None:
    import claude_tasks as T
    r = next((x for x in T.read_runs(root) if x.get("uuid") == uuid and x.get("kind") != "skipped"), None)
    if r is None:
        return None
    running = r.get("status") == "running" and T.pid_alive(r.get("pid"))
    status = r.get("status") if (r.get("status") != "running" or running) else "lost"
    rid = r.get("recording_id")
    metas = [m for m in store.items.values() if m.get("session_id") == rid or m.get("id") == rid]
    text = "\n".join(t.strip() for m in sorted(metas, key=lambda m: m.get("recorded_at") or "")
                     if (t := store.transcript_text(m)))
    lp = r.get("log_path") or ""
    try:
        log_lines = ANSI_RE.sub("", Path(lp).read_text(encoding="utf-8", errors="ignore")).splitlines()[-40:]
    except OSError:
        log_lines = []
    cid, path = _proposal(lp, workspace_path())
    prop = None
    if cid:
        try:
            prop = (workspace_path() / path).read_text(encoding="utf-8", errors="ignore")[:PROPOSAL_MAX]
        except OSError:
            pass
    return {"uuid": uuid, "name": f"aha-{rid}", "recording_id": rid, "kind": r.get("kind"), "status": status,
            "started": r.get("started"), "finished": r.get("finished"), "transcript": text[:6000],
            "change_id": cid, "proposal_path": path, "proposal": prop, "log_tail": [l[:400] for l in log_lines]}


def router(store, auth) -> APIRouter:
    r = APIRouter(prefix="/v1/claude", dependencies=[Depends(auth)])
    root = store.root

    @r.get("/sessions")
    async def sessions():
        try:
            board = await asyncio.to_thread(fetch_board)
        except Exception as e:
            raise HTTPException(502, f"tracker unavailable: {e}")
        return build_sessions(board, read_usage())

    @r.get("/usage")
    def usage():
        return read_usage()

    @r.get("/runs")
    async def runs():
        return await asyncio.to_thread(build_runs, store, root)

    @r.get("/sessions/{sid}")
    async def session_detail(sid: str):
        if not UUID_RE.match(sid):
            raise HTTPException(400, "bad session id")
        try:
            board = await asyncio.to_thread(fetch_board)
        except Exception:
            board = None  # transcript is still readable without the tracker
        d = await asyncio.to_thread(build_session_detail, sid, board)
        if d is None:
            raise HTTPException(404, "no such session")
        return d

    @r.get("/changes/{cid}")
    async def change_detail(cid: str):
        if not CHANGE_ID_RE.match(cid) or ".." in cid:
            raise HTTPException(400, "bad change id")
        d = await asyncio.to_thread(build_change, root, cid)
        if d is None:
            raise HTTPException(404, "no such change")
        return d

    @r.get("/runs/{uuid}")
    async def run_detail(uuid: str):
        if not RUN_ID_RE.match(uuid):
            raise HTTPException(400, "bad uuid")
        d = await asyncio.to_thread(build_run_detail, store, root, uuid)
        if d is None:
            raise HTTPException(404, "no such run")
        return d

    @r.post("/runs/{uuid}/stop")
    async def stop(uuid: str):
        import claude_tasks as T
        if not re.fullmatch(r"[0-9a-fA-F-]{8,36}", uuid):
            raise HTTPException(400, "bad uuid")
        msgs: list[str] = []
        try:
            rc = await asyncio.to_thread(T.kill_run, uuid, root, T.KILL_GRACE_S, msgs.append)
        except SystemExit as e:
            raise HTTPException(404, str(e))
        if rc != 0:
            raise HTTPException(409, msgs[-1] if msgs else "not running")
        return {"status": "killed", "uuid": uuid, "log": msgs}

    @r.get("/tasks")
    def tasks():
        return build_tasks(root)

    @r.post("/tasks/regroup", status_code=202)
    def regroup():
        if build_tasks(root)["regroup"]["state"] == "running":
            return {"status": "already_running"}
        p = subprocess.Popen([sys.executable, str(PIPELINE / "task_regroup.py"), "run"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                             close_fds=True, env={**os.environ, "AHA_DATA_DIR": str(root)})
        status_file(root).write_text(json.dumps({"state": "running", "pid": p.pid, "started": datetime.now().astimezone().isoformat(timespec="seconds")}))
        return {"status": "started"}

    return r
