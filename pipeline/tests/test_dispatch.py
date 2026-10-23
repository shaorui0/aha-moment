import json
import dispatch_quick as Q

R = Q.load_routes(Q.ROUTES)


def put(data, *recs, raw=None):
    ib = data / "inbox"; ib.mkdir(parents=True, exist_ok=True)
    with open(ib / "quick.jsonl", "a") as f:
        for i, t in recs:
            f.write(json.dumps({"id": i, "recorded_at": "2026-10-09T09:00:00+00:00", "text": t, "path": "/x"}, ensure_ascii=False) + "\n")
        if raw:
            f.write(raw)
    return ib


def test_match_rules():
    assert Q.match("待办，买牛奶", R)[1] == "买牛奶"
    assert Q.match("Todo: call bob", R)[0]["name"] == "todo"
    assert Q.match("记一下 灵感", R)[0]["name"] == "note"
    assert Q.match("问 Claude 什么是 CRDT", R)[1] == "什么是 CRDT"
    assert Q.match("ask what is 2+2", R)[0]["name"] == "ask"
    assert Q.match("asked for it", R)[0] is R["default"]       # word boundary
    assert Q.match("todolist app", R)[0] is R["default"]
    assert Q.match("随便说点什么", R)[0] is R["default"]


def test_routing_and_exactly_once(data, fake_claude):
    ib = put(data, ("id000001", "待办 买牛奶"), ("id000002", "note remember this"),
             ("id000003", "random"), ("id000004", "ask why sky blue"))
    assert Q.run(ib, R, log=lambda *_: None) == 4
    assert "买牛奶" in (ib / "todo.md").read_text()
    assert "remember this" in (ib / "notes.md").read_text()
    assert "random" in (ib / "unrouted.md").read_text()
    assert "STUB-ANSWER" in (ib / "answers" / "id000004.md").read_text()
    snap = {p.name: p.read_text() for p in ib.glob("*.md")}
    assert Q.run(ib, R, log=lambda *_: None) == 0                # re-run: nothing
    assert snap == {p.name: p.read_text() for p in ib.glob("*.md")}
    put(data, ("id000005", "待办 second"))                        # only the new line
    assert Q.run(ib, R, log=lambda *_: None) == 1
    assert (ib / "todo.md").read_text().count("- [ ]") == 2


def test_dry_run_no_side_effects(data, fake_claude):
    ib = put(data, ("id000001", "待办 x"), ("id000002", "ask y"))
    assert Q.run(ib, R, dry=True, log=lambda *_: None) == 2
    assert not (ib / ".cursor").exists() and not (ib / "todo.md").exists()
    assert not (ib / "answers").exists()
    assert Q.run(ib, R, log=lambda *_: None) == 2                # still pending for real run


def test_crash_between_effect_and_cursor_no_dup(data):
    ib = put(data, ("id000001", "待办 a"))
    Q.run(ib, R, log=lambda *_: None)
    (ib / ".cursor").unlink()                                    # simulate lost cursor
    Q.run(ib, R, log=lambda *_: None)
    assert (ib / "todo.md").read_text().count("id000001") == 1


def test_partial_line_waits(data):
    ib = put(data, ("id000001", "待办 a"), raw='{"id": "id000002", "text": "待办 b"')
    assert Q.run(ib, R, log=lambda *_: None) == 1
    with open(ib / "quick.jsonl", "a") as f:
        f.write(', "recorded_at": "2026-10-09T09:00:00Z"}\n')
    assert Q.run(ib, R, log=lambda *_: None) == 1
    assert "待办 b".split()[1] in (ib / "todo.md").read_text()


def test_bad_line_and_ask_failure(data, fake_claude, monkeypatch):
    ib = put(data, ("id000001", "ask hi"), raw="not json\n")
    monkeypatch.setenv("FAKE_RC", "2")
    assert Q.run(ib, R, log=lambda *_: None) == 1
    assert "ask failed" in (ib / "unrouted.md").read_text()
    assert (ib / "bad_lines.jsonl").read_text().strip() == "not json"
    assert not (ib / "answers" / "id000001.md").exists()


def test_empty_body_goes_default(data):
    ib = put(data, ("id000001", "待办"))
    Q.run(ib, R, log=lambda *_: None)
    assert (ib / "unrouted.md").exists() and not (ib / "todo.md").exists()


def test_missing_inbox_ok(data):
    assert Q.run(data / "inbox", R, log=lambda *_: None) == 0
