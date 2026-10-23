import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))
import extract_tasks as ET  # noqa: E402

MD = ("<!-- derived -->\n\n**[task]** 明天要把告警规则改一下。然后发给同事。\n\n**[thinking]** 想一想\n\n"
      "**[task]** " + "很长" * 30 + "\n\n## 纠正\n\n- 无\n")


def seed(client, items):   # v1 (flat, no version key)
    f = client.app.state.store.root / "inbox" / "tasks.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"items": items}))


def test_auth(client):
    assert TestClient(client.app).get("/v1/tasks").status_code == 401
    assert TestClient(client.app).post("/v1/tasks/ack", json={"ids": []}).status_code == 401


def v2(items):
    return {"version": 2, "items": items}


def seed2(client, items):
    f = client.app.state.store.root / "inbox" / "tasks.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(v2(items)))


def test_empty(client):
    r = client.get("/v1/tasks?undelivered=1").json()
    assert r["items"] == [] and "synth" in r


def test_list_ack_dismiss_v2(client):
    seed2(client, [{"id": "a", "title": "A", "status": "open", "next": "n", "evidence": [{"source": "s", "para": 0, "quote": "q"}]},
                   {"id": "b", "title": "B", "status": "reminded"}, {"id": "c", "title": "C", "status": "open"},
                   {"id": "d", "title": "D", "status": "dismissed"}])
    items = client.get("/v1/tasks?undelivered=1").json()["items"]
    assert [i["id"] for i in items] == ["a", "c"] and items[0]["note"].startswith("n") and items[0]["delivered"] is False
    assert len(client.get("/v1/tasks").json()["items"]) == 4
    assert client.post("/v1/tasks/ack", json={"ids": ["a", "zzz", "b"]}).json() == {"acked": 1}
    assert client.post("/v1/tasks/ack", json={"ids": ["a"]}).json() == {"acked": 0}
    assert client.post("/v1/tasks/dismiss", json={"ids": ["c", "d"]}).json() == {"dismissed": 1}   # d already dismissed
    assert client.get("/v1/tasks?undelivered=1").json()["items"] == []
    assert {i["id"]: i["status"] for i in client.get("/v1/tasks").json()["items"]} == \
        {"a": "reminded", "b": "reminded", "c": "dismissed", "d": "dismissed"}                      # kept, not deleted
    assert client.post("/v1/tasks/ack", json={"ids": "a"}).status_code == 400
    assert client.post("/v1/tasks/dismiss", json={"ids": "a"}).status_code == 400
    assert TestClient(client.app).post("/v1/tasks/dismiss", json={"ids": []}).status_code == 401


def test_v1_file_is_migrated_with_backup(client):
    seed(client, [{"id": "a", "title": "A", "note": "text\n\n— f.md", "source": "f.md", "delivered": False},
                  {"id": "b", "title": "B", "delivered": True}, {"id": "c", "title": "C", "dismissed": True},
                  {"id": "d", "title": "D", "stale": True}])
    got = {i["id"]: i["status"] for i in client.get("/v1/tasks").json()["items"]}
    assert got == {"b": "reminded", "c": "dismissed"}                  # open/stale segments become synthesis evidence
    inbox = client.app.state.store.root / "inbox"
    assert len(json.loads((inbox / "tasks.v1.json").read_text())["items"]) == 4
    assert [l["id"] for l in json.loads((inbox / "tasks.json").read_text())["legacy"]] == ["a"]


def test_to_claude_launches_and_guards(client, monkeypatch):
    import claude_tasks as T
    seed2(client, [{"id": "a", "title": "改告警", "next": "先看规则", "status": "open",
                    "evidence": [{"source": "s", "para": 0, "quote": "明天改告警"}]}])
    launched = []
    monkeypatch.setattr(T, "launch", lambda root, mode, u, rid, prompt: launched.append((mode, u, rid, prompt)))
    r = client.post("/v1/tasks/a/to_claude")
    assert r.status_code == 200 and r.json()["name"] == "aha-task-a"
    mode, u, rid, prompt = launched[0]
    assert mode == "new" and rid == "task-a" and "改告警" in prompt and "先看规则" in prompt and "明天改告警" in prompt
    it = client.get("/v1/tasks").json()["items"][0]
    assert it["status"] == "claude" and it["claude_uuid"] == u
    assert client.post("/v1/tasks/a/to_claude").status_code == 409
    assert client.post("/v1/tasks/nope/to_claude").status_code == 404


def test_to_claude_respects_run_limit(client, monkeypatch):
    import claude_tasks as T
    seed2(client, [{"id": "a", "title": "A", "status": "open"}])
    monkeypatch.setattr(T, "read_runs", lambda root=None: [{"status": "running", "pid": 1}] * T.MAX_RUNNING)
    monkeypatch.setattr(T, "pid_alive", lambda pid: True)
    assert client.post("/v1/tasks/a/to_claude").status_code == 409


def test_change_id_resolved_from_run_log(client, monkeypatch, tmp_path):
    import claude_tasks as T
    ws = tmp_path / "ws"
    (ws / "openspec/changes/my-change").mkdir(parents=True)
    (ws / "openspec/changes/my-change/proposal.md").write_text("# p")
    monkeypatch.setattr(T, "workspace", lambda: str(ws))
    root = client.app.state.store.root
    (root / "claude_runs").mkdir(parents=True)
    (root / "claude_runs/u1.log").write_text("wrote openspec/changes/my-change/proposal.md")
    seed2(client, [{"id": "a", "title": "A", "status": "claude", "claude_uuid": "u1"}])
    import tasks as SRV
    SRV._change_cache["at"] = 0.0
    assert client.get("/v1/tasks").json()["items"][0]["change_id"] == "my-change"


def test_extract(tmp_path):
    ddir = tmp_path / "daily_records"
    (ddir / "enhanced").mkdir(parents=True)
    (ddir / "enhanced" / "2026-10-16_2150_aha.enhance.md").write_text(MD)
    (ddir / "enhanced" / "2026-10-01_0900_aha.enhance.md").write_text(MD)
    out = tmp_path / "inbox" / "tasks.json"
    assert ET.run(ddir, {"2026-10-16"}, out)["added"] == 2
    items = json.loads(out.read_text())["items"]
    assert items[0]["title"] == "明天要把告警规则改一下"
    assert len(items[1]["title"]) == 40 and items[1]["title"].endswith("…")
    assert items[0]["created"] == "2026-10-16T21:50:00" and "2026-10-16_2150_aha.enhance.md" in items[0]["note"]
    items[0]["delivered"] = True
    out.write_text(json.dumps({"items": items}))
    assert ET.run(ddir, {"2026-10-16"}, out)["added"] == 0     # dedupe, delivered kept
    assert json.loads(out.read_text())["items"][0]["delivered"] is True
    assert ET.run(ddir, None, out)["added"] == 2                # --all picks up the other file


def test_extract_continuation_paragraphs_and_stale(tmp_path):
    ddir = tmp_path / "daily_records"
    (ddir / "enhanced").mkdir(parents=True)
    f = ddir / "enhanced" / "2026-10-16_2150_aha.enhance.md"
    f.write_text("**[task]** 第一段。\n\n第二段续写。\n\n**[thinking]** 想。\n\n续想。\n\n**[task]** 另一件。\n")
    out = tmp_path / "inbox" / "tasks.json"
    ET.run(ddir, None, out)
    items = json.loads(out.read_text())["items"]
    assert [i["note"].split("\n\n— ")[0] for i in items] == ["第一段。\n第二段续写。", "另一件。"]
    # old-name item with same text keeps id and gets the new source name; changed text is marked stale
    old_same = {"id": items[1]["id"], "title": "x", "note": "另一件。\n\n— 2026-10-16_2150_aha.md", "source": "2026-10-16_2150_aha.md", "delivered": False}
    old_gone = {"id": "oldoldoldold", "title": "gone", "note": "n", "source": "2026-10-16_2150_aha.md", "delivered": False}
    old_done = {"id": "doneddoneddn", "title": "d", "note": "n", "source": "2026-10-16_2150_aha.md", "delivered": True}
    out.write_text(json.dumps({"items": [old_same, old_gone, old_done]}))
    r = ET.run(ddir, None, out)
    got = {i["id"]: i for i in json.loads(out.read_text())["items"]}
    assert got["oldoldoldold"]["stale"] is True and "stale" not in got["doneddoneddn"]
    assert got[old_same["id"]]["source"].endswith(".enhance.md") and "stale" not in got[old_same["id"]]
    assert r["added"] == 1


def test_cut_natural_no_ellipsis():
    assert ET.cut_natural("短标题") == "短标题"
    t = ET.cut_natural("把告警规则改一下，然后发给同事再确认一下上线时间表")
    assert t == "把告警规则改一下" and "…" not in t
    assert len(ET.cut_natural("字" * 50)) == ET.TITLE_HARD


def test_llm_title_retries_when_too_long(monkeypatch):
    class G:
        calls = []
        @staticmethod
        def chat(model, msgs, opts):
            G.calls.append(msgs[0]["content"]); return {"n": len(G.calls)}
        @staticmethod
        def content(r):
            return "这是一个非常非常非常非常长的待办标题没完没了" if r["n"] == 1 else "改告警规则"
    assert ET.llm_title(G, "x") == "改告警规则" and len(G.calls) == 2


def test_resynthesize_starts_detached_and_guards(client, monkeypatch):
    import tasks as SRV
    class P:
        pid = 4242
    spawned = []
    monkeypatch.setattr(SRV.subprocess, "Popen", lambda *a, **k: spawned.append(a) or P())
    r = client.post("/v1/tasks/resynthesize")
    assert r.status_code == 202 and r.json() == {"status": "started"} and len(spawned) == 1
    import claude_tasks as T
    monkeypatch.setattr(T, "pid_alive", lambda pid: True)
    assert client.post("/v1/tasks/resynthesize").json() == {"status": "already_running"} and len(spawned) == 1
    assert client.get("/v1/tasks").json()["synth"]["state"] == "running"
