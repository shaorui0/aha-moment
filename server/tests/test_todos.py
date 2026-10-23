import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    w.mkdir()
    monkeypatch.setenv("AHA_VOICE_WORKSPACE", str(w))
    return w


def change(ws, cid, tasks, title=None, mtime=None):
    d = ws / "openspec/changes" / cid
    d.mkdir(parents=True)
    (d / "proposal.md").write_text(f"# Proposal: {title or cid}\nbody")
    if tasks is not None:
        (d / "tasks.md").write_text(tasks)
    if mtime:
        os.utime(d, (mtime, mtime))
        if tasks is not None:
            os.utime(d / "tasks.md", (mtime, mtime))
    return d


def write_json(client, name, obj):
    p = client.app.state.store.root / "inbox"
    p.mkdir(exist_ok=True)
    (p / name).write_text(json.dumps(obj) if not isinstance(obj, list) else "".join(json.dumps(r) + "\n" for r in obj))


def task(i, status, upd, **kw):
    return {"id": i, "title": f"t-{i}", "next": f"n-{i}", "area": "个人", "topic": "其他", "evidence": [], "status": status,
            "created": "2026-10-10T09:00:00", "updated": upd, **kw}


def groups(d):
    return {g["source"]: g["items"] for g in d["groups"]}


def test_endpoint_requires_auth_and_empty_state_is_200(client, ws):
    assert TestClient(client.app).get("/v1/todos").status_code == 401
    r = client.get("/v1/todos")
    assert r.status_code == 200
    d = r.json()
    assert d["groups"] == [] and d["summary"] == {"total_open": 0, "total_done_today": 0} and d["generated_at"]


def test_voice_synth_skips_done_dismissed_claude_and_orders_by_updated(client, ws):
    now = datetime.now().replace(microsecond=0)
    old, mid = (now - timedelta(days=3)).isoformat(), (now - timedelta(hours=2)).isoformat()
    write_json(client, "tasks.json", {"version": 2, "items": [
        task("a", "open", old), task("b", "reminded", mid, change_id="c1"), task("c", "done", now.isoformat()),
        task("d", "dismissed", mid), task("e", "claude", mid), task("f", "done", old), {"junk": True}]})
    d = client.get("/v1/todos").json()
    items = groups(d)["voice_synth"]
    assert [i["id"] for i in items] == ["b", "a"]                                   # updated desc, junk row skipped
    assert items[0] == {"kind": "voice_task", "id": "b", "title": "t-b", "next": "n-b", "area": "个人", "topic": "其他",
                        "status": "reminded", "source": "voice_synth", "created": "2026-10-10T09:00:00", "change_id": "c1"}
    assert "change_id" not in items[1]
    assert d["summary"] == {"total_open": 2, "total_done_today": 1}                 # only "c" was finished today


def test_openspec_rows_unlinked_go_to_other_with_progress(client, ws):
    change(ws, "half", "- [x] a\n- [ ] b\n- [ ] c\n", title="Half done", mtime=1_700_000_000)
    change(ws, "fresh", "- [ ] only\n", mtime=1_800_000_000)
    change(ws, "full", "- [x] a\n- [x] b\n", mtime=1_750_000_000)                   # complete -> not a todo
    change(ws, "notasks", None)                                                     # no tasks.md -> not a todo
    (ws / "openspec/changes/archive/old").mkdir(parents=True)                       # archive dir is not a change
    d = client.get("/v1/todos").json()
    assert [g["source"] for g in d["groups"]] == ["other_openspec"]
    rows = groups(d)["other_openspec"]
    assert [r["id"] for r in rows] == ["fresh", "half"]                             # newest activity first
    assert rows[1] == {"kind": "openspec", "id": "half", "title": "Half done", "done": 1, "total": 3, "percent": 33,
                       "change_path": "openspec/changes/half", "source": "other", "source_hint": "manual"}
    assert d["summary"]["total_open"] == 2


def test_voice_trigger_groups_runs_by_change_and_hides_them_from_other(client, ws, tmp_path):
    change(ws, "from-voice", "- [x] a\n- [ ] b\n", title="Voice made")
    change(ws, "from-voice-2", "- [ ] a\n")
    change(ws, "manual", "- [ ] a\n")
    change(ws, "voice-finished", "- [x] a\n")                                       # run exists but nothing left to do
    logs = {}
    for name, cid in (("r1", "from-voice"), ("r2", "from-voice-2"), ("r3", "voice-finished"), ("r4", "from-voice")):
        logs[name] = tmp_path / f"{name}.log"
        logs[name].write_text(f"wrote openspec/changes/{cid}/proposal.md")
    mk = lambda u, rec, name, started, **kw: {"uuid": u, "pid": None, "recording_id": rec, "started": started, "status": "done",  # noqa: E731
                                              "log_path": str(logs[name]), "kind": "new", **kw}
    write_json(client, "claude_runs.jsonl", [
        mk("11111111-aaaa", "rec1", "r1", "2026-10-16T21:00:00+09:00"),
        mk("22222222-aaaa", "rec2", "r2", "2026-10-17T08:00:00+09:00"),
        mk("33333333-aaaa", "rec3", "r3", "2026-10-17T09:00:00+09:00"),
        mk("44444444-aaaa", "rec4", "r4", "2026-10-17T10:00:00+09:00"),              # newest run for from-voice wins
        mk("55555555-aaaa", "rec5", "r1", "2026-10-17T11:00:00+09:00", kind="skipped")])
    d = client.get("/v1/todos").json()
    assert [g["source"] for g in d["groups"]] == ["voice_trigger", "other_openspec"]
    trig = groups(d)["voice_trigger"]
    assert [(r["id"], r["run_name"], r["source"]) for r in trig] == [
        ("from-voice", "aha-rec4", "voice_trigger"), ("from-voice-2", "aha-rec2", "voice_trigger")]   # started desc
    assert trig[0]["started"] == "2026-10-17T10:00:00+09:00" and trig[0]["title"] == "Voice made"
    assert (trig[0]["done"], trig[0]["total"], trig[0]["percent"], trig[0]["kind"]) == (1, 2, 50, "openspec")
    assert [r["id"] for r in groups(d)["other_openspec"]] == ["manual"]
    assert d["summary"]["total_open"] == 3


def test_nested_change_from_tree_and_unreadable_items_are_skipped(client, ws):
    nested = ws / "projects/p/openspec/changes/deep"
    nested.mkdir(parents=True)
    (nested / "tasks.md").write_text("- [ ] x\n- [ ] y\n")
    change(ws, "ok", "- [ ] a\n")
    bad = change(ws, "unreadable", "- [ ] a\n")
    (bad / "tasks.md").unlink()
    (bad / "tasks.md").mkdir()                                                      # tasks.md is a dir -> read fails
    (client.app.state.store.root / "claude_tasks_tree.json").write_text(json.dumps(
        [{"area": "个人", "group": "G", "changes": [{"id": "deep", "path": "projects/p/openspec/changes/deep"},
                                                    {"id": "gone", "path": "nope/openspec/changes/gone"}]}]))
    write_json(client, "tasks.json", {"version": 2, "items": [task("a", "open", "bad-date")]})
    d = client.get("/v1/todos").json()
    assert {r["id"] for r in groups(d)["other_openspec"]} == {"ok", "deep"}
    assert groups(d)["other_openspec"][0]["id"] != "unreadable"
    assert [i["id"] for i in groups(d)["voice_synth"]] == ["a"]
    deep = next(r for r in groups(d)["other_openspec"] if r["id"] == "deep")
    assert deep["change_path"] == "projects/p/openspec/changes/deep" and deep["title"] == "deep"


def test_summary_counts_openspec_finished_today_and_ignores_v1_tasks_doc(client, ws):
    change(ws, "finished-today", "- [x] a\n- [x] b\n")                              # mtime = now
    change(ws, "finished-long-ago", "- [x] a\n", mtime=1_600_000_000)
    write_json(client, "tasks.json", {"items": [{"id": "x", "title": "v1 segment", "note": "n", "delivered": False}]})
    d = client.get("/v1/todos").json()
    assert d["groups"] == [] and d["summary"] == {"total_open": 0, "total_done_today": 1}
