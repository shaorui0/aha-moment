import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synthesize_tasks as SY  # noqa: E402
import tasks_store as S  # noqa: E402

MD = ("<!-- derived -->\n\n**[task]** 明天要把告警规则改一下，然后发给同事看。\n\n接着还要订机票去东京。\n\n"
      "**[smalltalk]** 嗯对对对对对对对对\n\n**[thinking]** 我在想要不要把 Aha 的 API 评审一下再动手。\n\n## 纠正\n\n- 无\n")


@pytest.fixture
def env(tmp_path):
    ddir = tmp_path / "daily_records"
    (ddir / "enhanced").mkdir(parents=True)
    (ddir / "enhanced" / "2026-10-16_2150_aha.enhance.md").write_text(MD)
    data = tmp_path / "data"
    out = data / "inbox" / "tasks.json"
    ws = tmp_path / "ws"
    (ws / "tools/kb_catalog").mkdir(parents=True)
    (ws / "tools/kb_catalog/topics.yml").write_text("topics:\n  - name: 告警治理\n    aliases: [告警]\n  - name: 日本生活\n")
    return ddir, out, data, ws


def model_for(*payloads):
    calls = iter(payloads)
    prompts = []
    def m(prompt):
        prompts.append(prompt)
        p = next(calls)
        return p if isinstance(p, str) else json.dumps(p, ensure_ascii=False)
    m.prompts = prompts
    return m


def run(env, model, days=0):
    ddir, out, data, ws = env
    return SY.synthesize(ddir, out, days, ws=ws, model=model, data=data)


def test_paragraphs_inherit_labels_and_skip_smalltalk(env):
    ddir = env[0]
    ev = SY.gather(ddir, 0, set())
    assert [(e["para"], e["label"]) for e in ev] == [(0, "task"), (1, "task"), (3, "thinking")]   # smalltalk dropped


def test_creates_split_and_validates(env):
    m = model_for({"tasks": [
        {"id": None, "title": "修改告警规则并发给同事看一下谢谢", "next": "改完发同事", "area": "工作", "topic": "告警治理", "evidence": ["E1"]},
        {"id": None, "title": "订去东京的机票", "next": "查机票", "area": "个人", "topic": "不存在的topic", "evidence": ["E2", "E99"]},
        {"id": None, "title": "没有证据", "evidence": []},
        {"id": None, "title": "坏 area", "area": "x", "evidence": ["E3"]},
        "garbage"]})
    res = run(env, m)
    assert res["created"] == 3
    items = {i["title"]: i for i in S.load_doc(env[1])["items"]}
    assert "没有证据" not in items
    long = next(i for i in items.values() if i["title"].startswith("修改告警"))
    assert len(long["title"]) <= 20 and long["topic"] == "告警治理" and long["status"] == "open"
    assert long["evidence"][0]["quote"].startswith("明天要把告警") and long["evidence"][0]["source"].endswith(".enhance.md")
    trip = items["订去东京的机票"]
    assert trip["topic"] == "其他" and [e["para"] for e in trip["evidence"]] == [1]       # E99 dropped
    assert items["坏 area"]["area"] == "个人"
    assert "告警治理" in m.prompts[0] and "日本生活" in m.prompts[0]


def test_merge_keeps_id_and_attaches_evidence_then_seen_skips(env):
    ddir, out, data, ws = env
    run(env, model_for({"tasks": [{"id": None, "title": "修改告警规则", "next": "a", "area": "工作", "topic": "告警治理", "evidence": ["E1"]}]}))
    tid = S.load_doc(out)["items"][0]["id"]
    (ddir / "enhanced" / "2026-10-16_2200_aha.enhance.md").write_text("**[task]** 告警规则改完之后还要加一个静默窗口。\n")
    m = model_for({"tasks": [{"id": tid, "title": "ignored new title", "next": "加静默窗口", "area": "工作", "topic": "告警治理", "evidence": ["E1"]}]})
    res = run(env, m)
    items = S.load_doc(out)["items"]
    assert res["merged"] == 1 and res["created"] == 0 and len(items) == 1
    assert items[0]["id"] == tid and items[0]["title"] == "修改告警规则" and items[0]["next"] == "加静默窗口"
    assert len(items[0]["evidence"]) == 2
    assert tid in m.prompts[0] and "修改告警规则" in m.prompts[0]                      # open tasks are shown to the model
    called = []
    assert run(env, lambda p: called.append(p) or '{"tasks": []}')["evidence"] == 0 and not called   # nothing new -> no model call


def test_negative_list_respected_and_shown(env):
    ddir, out, data, ws = env
    S.save_doc(out, {"version": 2, "items": [
        {"id": "x1", "title": "订去东京的机票", "status": "dismissed", "evidence": [], "next": "", "area": "个人", "topic": "其他"},
        {"id": "x2", "title": "已推送的事", "status": "reminded", "evidence": []}]})
    m = model_for({"tasks": [{"id": None, "title": "订去东京的机票", "next": "n", "area": "个人", "topic": "其他", "evidence": ["E2"]}]})
    res = run(env, m)
    assert res["created"] == 0 and res["skipped"] == 1
    assert "订去东京的机票" in m.prompts[0] and "已推送的事" in m.prompts[0]
    assert len(S.load_doc(out)["items"]) == 2


def test_bad_json_retried_once_then_raises(env):
    m = model_for("not json", {"tasks": []})
    assert run(env, m)["created"] == 0 and len(m.prompts) == 2 and "不是合法 JSON" in m.prompts[1]
    with pytest.raises(ValueError):
        SY.parse_tasks("nope")


def test_migration_keeps_flags_backup_and_feeds_legacy(env):
    ddir, out, data, ws = env
    out.parent.mkdir(parents=True)
    out.write_text(json.dumps({"items": [
        {"id": "a", "title": "A", "note": "旧的开放段落文字\n\n— 2026-10-01_0900_aha.md", "source": "2026-10-01_0900_aha.md", "created": "2026-10-01T09:00:00", "delivered": False},
        {"id": "b", "title": "B", "note": "n", "delivered": True}, {"id": "c", "title": "C", "note": "n", "dismissed": True}]}))
    m = model_for({"tasks": []})
    run(env, m)
    doc = S.load_doc(out)
    assert doc["version"] == 2 and {i["id"]: i["status"] for i in doc["items"]} == {"b": "reminded", "c": "dismissed"}
    assert (out.parent / "tasks.v1.json").exists() and len(json.loads((out.parent / "tasks.v1.json").read_text())["items"]) == 3
    assert "旧的开放段落文字" in m.prompts[0] and "B" in m.prompts[0] and "C" in m.prompts[0]   # legacy evidence + negative list
    assert S.migrate_v1(out) is False                                                       # idempotent


def test_user_changes_during_model_call_survive(env):
    ddir, out, data, ws = env
    S.save_doc(out, {"version": 2, "items": [{"id": "t1", "title": "T1", "status": "open", "evidence": [], "next": "", "area": "个人", "topic": "其他"}]})
    def model(prompt):
        doc = S.load_doc(out); doc["items"][0]["status"] = "dismissed"; S.save_doc(out, doc)   # user swipes while the model runs
        return json.dumps({"tasks": [{"id": "t1", "title": "T1", "next": "n", "area": "个人", "topic": "其他", "evidence": ["E1"]}]})
    run(env, model)
    items = {i["title"]: i for i in S.load_doc(out)["items"]}
    assert items["T1"]["status"] == "dismissed"                                              # not resurrected / not merged into


def test_daily_guard(env, monkeypatch):
    ddir, out, data, ws = env
    monkeypatch.setenv("AHA_DATA_DIR", str(data))
    monkeypatch.setenv("AHA_CONTEXT_ROOT", str(ddir.parents[0]))
    SY.write_status({"last_ok": SY.now_iso()}, data)
    monkeypatch.setattr(SY, "synthesize", lambda *a, **k: pytest.fail("must not run twice a day"))
    assert SY.main(["--daily"]) == 0
