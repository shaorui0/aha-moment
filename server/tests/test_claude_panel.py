import json
import time
from datetime import datetime, timezone

import pytest

import claude_panel as P

NOW = time.time()
ISO = lambda dt: datetime.fromtimestamp(dt, timezone.utc).isoformat().replace("+00:00", "Z")  # noqa: E731


def sess(sid, phase, age_s, **kw):
    return {"session_id": sid, "name": sid, "phase": phase, "phase_since": ISO(NOW - age_s),
            "status_updated_at": ISO(NOW - age_s), "change_ids": [], "todo_items": [],
            "last_human_prompt": "q" * 300, "last_text": "a", "last_action": "x", **kw}


BOARD = {"workspaces": [
    {"name": "context-infrastructure", "changes": [{"id": "c1", "progress": {"done": 1, "total": 2},
        "items": [{"text": "t1 | owns: x", "done": True}, {"text": "t2", "done": False, "current": True}]}],
     "sessions": [sess("old", "done", 3600 * 30), sess("d1", "exited", 60), sess("r1", "running", 5),
                  sess("n1", "needs_input", 120, waiting_for="ok?"), sess("c", "done", 300, change_ids=["c1"])]},
    {"name": "other", "changes": [], "sessions": [sess("o", "running", 1)]}]}


@pytest.fixture(autouse=True)
def tracker(monkeypatch, tmp_path):
    monkeypatch.setattr(P, "fetch_board", lambda: BOARD)
    u = tmp_path / "usage.json"
    u.write_text(json.dumps({"ok": True, "windows": [{"id": "five_hour", "percent": 24, "resets_at": int(NOW + 3600)}]}))
    monkeypatch.setenv("AHA_TRACKER_USAGE", str(u))
    monkeypatch.setenv("AHA_VOICE_WORKSPACE", str(tmp_path / "ws"))


def test_auth(client):
    from fastapi.testclient import TestClient
    c = TestClient(client.app)
    for path in ("sessions", "usage", "runs", "tasks"):
        assert c.get(f"/v1/claude/{path}").status_code == 401
    assert c.post("/v1/claude/tasks/regroup").status_code == 401
    assert c.post("/v1/claude/runs/abcdef12/stop").status_code == 401


def test_sessions_filter_order_state(client):
    d = client.get("/v1/claude/sessions").json()
    assert [s["sid"] for s in d["sessions"]] == ["r1", "n1", "d1", "c"]        # other ws + old session dropped
    assert [s["state"] for s in d["sessions"]] == ["running", "needs_input", "done", "done"]
    s = d["sessions"][0]
    assert len(s["last_user"]) == 201 and s["last_user"].endswith("…")
    c = d["sessions"][3]
    assert c["progress"] == {"done": 1, "total": 2} and c["tasks"][0]["text"] == "t1" and c["tasks"][1]["current"]


def test_usage(client):
    assert client.get("/v1/claude/usage").json()["windows"][0]["percent"] == 24


def test_tracker_down_is_502(client, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(P, "fetch_board", boom)
    assert client.get("/v1/claude/sessions").status_code == 502


def _runs(client, rows):
    p = client.app.state.store.root / "inbox"
    p.mkdir(exist_ok=True)
    (p / "claude_runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_runs_and_stop(client, tmp_path):
    ws = tmp_path / "ws"
    (ws / "openspec/changes/foo").mkdir(parents=True)
    (ws / "openspec/changes/foo/proposal.md").write_text("x")
    log = tmp_path / "r.log"
    log.write_text("wrote openspec/changes/foo/proposal.md and openspec/changes/gone/proposal.md")
    u1, u2, u3 = "11111111-aaaa", "22222222-aaaa", "33333333-aaaa"
    _runs(client, [
        {"uuid": u1, "pid": None, "recording_id": "rec1", "started": "2026-10-16T21:00:00+09:00", "status": "done",
         "finished": "2026-10-16T21:01:00+09:00", "log_path": str(log), "kind": "new"},
        {"uuid": u2, "pid": 2 ** 22 + 7, "recording_id": "rec2", "started": "2026-10-16T22:00:00+09:00",
         "status": "running", "log_path": str(log), "kind": "new"},
        {"uuid": u1, "pid": None, "recording_id": "rec3", "status": "skipped", "kind": "skipped"}])
    runs = client.get("/v1/claude/runs").json()["runs"]
    assert [r["name"] for r in runs] == ["aha-rec2", "aha-rec1"]               # newest first, skipped hidden
    assert runs[0]["status"] == "lost"                                          # dead pid
    assert runs[1]["proposal_path"] == "openspec/changes/foo/proposal.md" and runs[1]["change_id"] == "foo"
    assert client.post(f"/v1/claude/runs/{u3}/stop").status_code == 404
    assert client.post("/v1/claude/runs/xx/stop").status_code == 400
    assert client.post(f"/v1/claude/runs/{u1}/stop").status_code == 409         # not running


def test_stop_kills_via_claude_tasks(client, monkeypatch):
    import claude_tasks as T
    calls = []
    monkeypatch.setattr(T, "kill_run", lambda which, root, grace, out: (calls.append(which), out("killed"), 0)[2])
    r = client.post("/v1/claude/runs/11111111-aaaa/stop")
    assert r.status_code == 200 and calls == ["11111111-aaaa"]


def test_tasks_tree_and_regroup(client, monkeypatch):
    root = client.app.state.store.root
    assert client.get("/v1/claude/tasks").json()["groups"] == []
    (root / "claude_tasks_tree.json").write_text(json.dumps([{"area": "个人", "group": "G", "changes": []}]))
    d = client.get("/v1/claude/tasks").json()
    assert d["groups"][0]["group"] == "G" and d["generated_at"]
    spawned = []

    class FakeP:
        pid = 12345
    monkeypatch.setattr(P.subprocess, "Popen", lambda *a, **k: spawned.append(a[0]) or FakeP())
    assert client.post("/v1/claude/tasks/regroup").json() == {"status": "started"}
    assert spawned[0][1].endswith("task_regroup.py")
    # fake pid is not alive -> reported as error, not stuck on running
    assert client.get("/v1/claude/tasks").json()["regroup"]["state"] == "error"


# ---------------------------------------------------------------- detail endpoints
SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _transcript(tmp_path, monkeypatch, sid=SID):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    rows = [{"type": "user", "message": {"content": "hello " + "u" * 600}, "timestamp": "t1"},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}, {"type": "tool_use"}]}},
            {"type": "assistant", "isSidechain": True, "message": {"content": "sub"}}]
    rows += [{"type": "user", "message": {"content": f"m{i}"}} for i in range(12)]
    (d / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setenv("AHA_CLAUDE_PROJECTS", str(d))


def test_detail_auth(client):
    from fastapi.testclient import TestClient
    c = TestClient(client.app)
    for path in (f"sessions/{SID}", "changes/foo", "runs/11111111-aaaa"):
        assert c.get(f"/v1/claude/{path}").status_code == 401


def test_session_detail(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AHA_CLAUDE_PROJECTS", str(tmp_path / "empty"))
    assert client.get(f"/v1/claude/sessions/{SID}").status_code == 404
    _transcript(tmp_path, monkeypatch)
    d = client.get(f"/v1/claude/sessions/{SID}").json()
    assert len(d["messages"]) == 10 and d["messages"][-1]["text"] == "m11" and d["workspace"].endswith("ws")
    first = P.read_transcript_tail(tmp_path / "proj" / f"{SID}.jsonl", n=20)
    assert first[0]["text"].endswith("…") and len(first[0]["text"]) == 501 and first[1]["role"] == "assistant"
    assert len(first) == 14                                                     # tool_result + sidechain skipped


def test_session_detail_traversal_and_meta(client, tmp_path, monkeypatch):
    _transcript(tmp_path, monkeypatch)
    for bad in ("..%2f..%2fetc%2fpasswd", "not-a-uuid", "c", SID + "x"):
        assert client.get(f"/v1/claude/sessions/{bad}").status_code in (400, 404)
    assert client.get("/v1/claude/sessions/not-a-uuid").status_code == 400
    board_sid = "11111111-2222-3333-4444-555555555555"
    BOARD["workspaces"][0]["sessions"].append(sess(board_sid, "running", 5, session_id=board_sid, change_ids=["c1"]))
    try:
        d = client.get(f"/v1/claude/sessions/{board_sid}").json()
    finally:
        BOARD["workspaces"][0]["sessions"].pop()
    assert d["state"] == "running" and d["change_links"][0]["id"] == "c1" and d["messages"] == []


def test_change_detail(client, tmp_path):
    ch = tmp_path / "ws/openspec/changes/foo"
    ch.mkdir(parents=True)
    (ch / "proposal.md").write_text("# Proposal: Foo\n" + "x" * 9000)
    (ch / "tasks.md").write_text("- [x] a\n- [ ] b\nnot a task\n  - [X] c\n")
    d = client.get("/v1/claude/changes/foo").json()
    assert d["title"] == "Foo" and len(d["proposal"]) == 8000 and d["truncated"]
    assert d["tasks"] == [{"text": "a", "done": True}, {"text": "b", "done": False}, {"text": "c", "done": True}]
    assert (d["done"], d["total"]) == (2, 3)
    assert client.get("/v1/claude/changes/nope").status_code == 404
    assert client.get("/v1/claude/changes/a..b").status_code == 400
    assert client.get("/v1/claude/changes/.hidden").status_code == 400
    assert client.get("/v1/claude/changes/..%2f..%2fetc").status_code in (400, 404)
    (tmp_path / "outside").mkdir()
    (tmp_path / "ws/openspec/changes/evil").symlink_to(tmp_path / "outside")   # escaping symlink refused
    assert client.get("/v1/claude/changes/evil").status_code == 404


def test_run_detail(client, tmp_path):
    ws = tmp_path / "ws"
    (ws / "openspec/changes/foo").mkdir(parents=True)
    (ws / "openspec/changes/foo/proposal.md").write_text("# P\nbody")
    log = tmp_path / "r.log"
    log.write_text("\n".join(f"\x1b[31mline{i}\x1b[0m" for i in range(60)) + "\nopenspec/changes/foo/proposal.md\n")
    u = "11111111-aaaa"
    _runs(client, [{"uuid": u, "pid": None, "recording_id": "rec1", "started": "s", "status": "done", "finished": "f",
                    "log_path": str(log), "kind": "new"}])
    d = client.get(f"/v1/claude/runs/{u}").json()
    assert d["status"] == "done" and d["change_id"] == "foo" and d["proposal"].startswith("# P")
    assert len(d["log_tail"]) == 40 and "\x1b" not in "".join(d["log_tail"]) and d["log_tail"][-1].endswith("proposal.md")
    assert client.get("/v1/claude/runs/33333333-aaaa").status_code == 404
    assert client.get("/v1/claude/runs/xx").status_code == 400
    assert client.get("/v1/claude/runs/..%2f..%2fetc").status_code in (400, 404)


def test_session_title_prefers_custom_title(tmp_path, monkeypatch):
    import server.claude_panel as cp
    monkeypatch.setenv("AHA_CLAUDE_PROJECTS", str(tmp_path))
    sid = "11111111-2222-3333-4444-555555555555"
    (tmp_path / f"{sid}.jsonl").write_text(
        '{"type":"ai-title","aiTitle":"auto name"}\n{"type":"custom-title","customTitle":"my \\"renamed\\""}\n'
        '{"type":"ai-title","aiTitle":"later auto"}\n', encoding="utf-8")
    cp._title_cache.clear()
    assert cp.session_title(sid) == 'my "renamed"'
    assert cp.session_title("00000000-0000-0000-0000-000000000000") is None
