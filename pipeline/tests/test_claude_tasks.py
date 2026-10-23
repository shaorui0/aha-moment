import json, os, signal, subprocess, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import claude_tasks as T

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def ctx_ws(tmp_path, monkeypatch):
    """Isolated workspace; enhance wait disabled by default (old tests fall back to raw immediately)."""
    ws = tmp_path / "ws"
    monkeypatch.setenv("AHA_CONTEXT_ROOT", str(ws))
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 0)
    monkeypatch.setattr(T, "trigger_enhance", lambda *a, **k: None)
    return ws


def mk(root, iid, text, tags=None, sess=None, status="done", received="2026-10-16T11:00:00+00:00", day="2026/10/16", rec="2026-10-16T10:50:00+00:00"):
    d = root / day / iid
    d.mkdir(parents=True, exist_ok=True)
    meta = {"id": iid, "source": "ambient" if sess else "quick", "recorded_at": rec, "received_at": received,
            "transcription_status": status, "transcripts": [{"slug": "m1"}] if text is not None else []}
    if tags is not None: meta["tags"] = tags
    if sess: meta["session_id"] = sess
    (d / "meta.json").write_text(json.dumps(meta))
    if text is not None: (d / "transcript.m1.txt").write_text(text)


def test_selection_and_grouping(data):
    mk(data, "a1", "第一段", ["claude"], "S1", rec="2026-10-16T10:50:00+00:00")
    mk(data, "a2", "第二段", None, "S1", rec="2026-10-16T10:51:00+00:00")      # untagged chunk of tagged session
    mk(data, "b1", "没打标签", None, "S2")
    mk(data, "q1", "quick 任务", ["claude"])                                    # quick: no session
    mk(data, "c1", "还在转写", ["claude"], "S3", status="pending")
    mk(data, "d1", "刚收到", ["claude"], "S4", received="2026-10-16T11:59:00+00:00")  # tail not quiet yet
    mk(data, "e1", None, ["claude"], "S5", status="silent")                      # only silence: nothing to run
    got = T.scan_sessions(data, NOW)
    by = {g["recording_id"]: g["text"] for g in got}
    assert by == {"S1": "第一段\n第二段", "q1": "quick 任务"}
    mk(data, "c1", "还在转写", ["claude"], "S3", status="done")
    assert "S3" in {g["recording_id"] for g in T.scan_sessions(data, NOW)}


def test_parse_reply():
    assert T.parse_reply("回复上一个，继续做第二步") == "继续做第二步"
    assert T.parse_reply("回复：好的") == "好的"
    assert T.parse_reply("  回复 ") == ""
    assert T.parse_reply("我想回复一下") is None


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """claude stub that records its argv to a file and exits (or sleeps when FAKE_SLEEP)."""
    p = tmp_path / "claude"
    p.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/argv.$$"\n[ -n "$FAKE_SLEEP" ] && exec sleep "$FAKE_SLEEP"\n'
                 'echo cwd=$(pwd) voice=$AHA_VOICE_RUN\n')
    p.chmod(0o755)
    monkeypatch.setenv("AHA_CLAUDE_BIN", str(p))
    monkeypatch.setenv("AHA_VOICE_WORKSPACE", str(tmp_path))
    return p


def wait_status(root, pred, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        runs = T.read_runs(root)
        if runs and pred(runs):
            return runs
        time.sleep(0.1)
    raise AssertionError(T.read_runs(root))


def test_new_run_then_reply_routing(data, stub, tmp_path):
    mk(data, "q1", "给 dispatch 加个重试", ["claude"])
    assert T.scan(data, NOW, log=lambda *_: None) == 1
    runs = wait_status(data, lambda r: r[0]["status"] != "running")
    r = runs[0]
    assert r["status"] == "done" and r["exit_code"] == 0 and r["recording_id"] == "q1" and r["kind"] == "new"
    assert Path(r["log_path"]).read_text().count("voice=1") == 1
    argv = (next(tmp_path.glob("argv.*"))).read_text()
    assert f"--session-id {r['uuid']}" in argv and "给 dispatch 加个重试" in argv and "proposal" in argv
    assert "--strict-mcp-config" in argv and "--disallowedTools" in argv
    assert T.scan(data, NOW, log=lambda *_: None) == 0                        # idempotent per recording
    for f in tmp_path.glob("argv.*"): f.unlink()
    # a reply recording resumes the latest run's uuid
    mk(data, "q2", "回复上一个 把范围缩小一点", ["claude"], rec="2026-10-16T11:00:00+00:00")
    assert T.scan(data, NOW, log=lambda *_: None) == 1
    runs = wait_status(data, lambda r: len(r) == 2 and r[1]["status"] != "running")
    assert runs[1]["uuid"] == r["uuid"] and runs[1]["kind"] == "reply" and runs[1]["recording_id"] == "q2"
    argv = (next(tmp_path.glob("argv.*"))).read_text()
    assert f"--resume {r['uuid']}" in argv and "--session-id" not in argv and "把范围缩小一点" in argv
    assert "OpenSpec" not in argv                                              # reply text passed as is


def test_reply_deferred_while_running_and_without_previous(data, stub, tmp_path, monkeypatch):
    mk(data, "q0", "回复 随便", ["claude"])                                   # no previous -> new task
    T.scan(data, NOW, log=lambda *_: None)
    r = wait_status(data, lambda r: r[0]["status"] != "running")[0]
    assert r["kind"] == "new"
    monkeypatch.setenv("FAKE_SLEEP", "30")
    mk(data, "q1", "长任务", ["claude"], rec="2026-10-16T11:01:00+00:00")
    T.scan(data, NOW, log=lambda *_: None)
    mk(data, "q2", "回复 继续", ["claude"], rec="2026-10-16T11:02:00+00:00")
    assert T.scan(data, NOW, log=lambda *_: None) == 0                        # latest still running: deferred
    assert len(T.read_runs(data)) == 2
    T.kill_run("last", data, grace=5, out=lambda *_: None)
    assert T.read_runs(data)[-1]["status"] == "killed"


def test_kill_escalates_to_sigkill(data, stub, monkeypatch):
    # stub that ignores SIGTERM: supervisor dies on TERM but child must still be reaped via SIGKILL of the group
    p = Path(os.environ["AHA_CLAUDE_BIN"])
    p.write_text('#!/bin/sh\ntrap "" TERM\nwhile true; do sleep 1; done\n')
    mk(data, "q1", "x", ["claude"])
    T.scan(data, NOW, log=lambda *_: None)
    pid = wait_status(data, lambda r: r[0]["pid"])[0]["pid"]
    time.sleep(0.5)
    out = []
    t0 = time.time()
    assert T.kill_run("last", data, grace=1.5, out=out.append) == 0
    assert any("SIGKILL" in o for o in out) and time.time() - t0 < 6
    assert T.read_runs(data)[-1]["status"] == "killed"
    with pytest.raises(ProcessLookupError):
        os.killpg(os.getpgid(pid), 0)


def test_lost_run_reconciled(data):
    T.append_run({"uuid": "u", "pid": 999999, "recording_id": "r", "started": "x", "status": "running",
                  "exit_code": None, "log_path": "l", "kind": "new"}, data)
    T.reconcile(data)
    assert T.read_runs(data)[0]["status"] == "lost"


def test_quick_dispatch_skips_tagged(data):
    import dispatch_quick as Q
    ib = data / "inbox"; ib.mkdir(parents=True)
    (ib / "quick.jsonl").write_text(json.dumps({"id": "q1", "recorded_at": "2026-10-16T10:00:00+00:00",
                                                "text": "待办 买菜", "tags": ["claude"]}, ensure_ascii=False) + "\n")
    Q.run(ib, Q.load_routes(Q.ROUTES), log=lambda *_: None)
    assert not (ib / "todo.md").exists() and not (ib / "unrouted.md").exists()


def test_late_tagging_triggers_once(data, stub):
    """Recording transcribed long ago, tagged afterwards (server merges tags into meta.json): picked up once."""
    mk(data, "l1", "先说第一段", None, "SL", received="2026-10-16T09:00:00+00:00", rec="2026-10-16T08:50:00+00:00")
    mk(data, "l2", "再说第二段", None, "SL", received="2026-10-16T09:01:00+00:00", rec="2026-10-16T08:51:00+00:00")
    assert T.scan(data, NOW, log=lambda *_: None) == 0                        # untagged: nothing
    for iid in ("l1", "l2"):                                                  # late tag, as POST /v1/sessions/SL/tags does
        mp = data / "2026/10/16" / iid / "meta.json"
        m = json.loads(mp.read_text()); m["tags"] = ["claude"]; mp.write_text(json.dumps(m))
    assert T.scan(data, NOW, log=lambda *_: None) == 1
    wait_status(data, lambda r: r[0]["status"] != "running")
    assert T.scan(data, NOW, log=lambda *_: None) == 0                        # once only
    assert [r["recording_id"] for r in T.read_runs(data)] == ["SL"]


# ---------------------------------------------------------------- corrected-text round
ENH = ("<!-- derived from x; model m; do not edit -->\n\n**[task]** 给 dispatch 加个重试。\n\n第二段同标签无标记。\n\n"
       "**[smalltalk]** 今天天气不错。\n\n还是闲聊续段。\n\n**[thinking]** 想一下范围。\n\n## 纠正\n\n- 低八 → dispatch\n")


def mk_enh(ws, name="2026-10-16_1050_aha", enh=ENH, src_text="给低八加个重试", enh_newer=True):
    d = ws / "contexts" / "daily_records"
    (d / "enhanced").mkdir(parents=True, exist_ok=True)
    src = d / f"{name}.md"
    src.write_text(src_text + "\n")
    if enh is not None:
        e = d / "enhanced" / f"{name}.enhance.md"
        e.write_text(enh)
        os.utime(src, (1000, 1000))
        os.utime(e, (2000, 2000) if enh_newer else (500, 500))
    return src


def rec_for(data, text="给低八加个重试", ready_s_ago=0):
    mk(data, "q1", text, ["claude"], rec="2026-10-16T10:50:00+00:00",
       received=(NOW - timedelta(seconds=ready_s_ago)).isoformat())
    return T.scan_sessions(data, NOW)[0]


def prompts(tmp_path):
    return [f.read_text() for f in tmp_path.glob("argv.*")]


def test_parse_enhanced_drops_markers_comment_corrections_smalltalk():
    out = T.parse_enhanced(ENH)
    assert out == "给 dispatch 加个重试。\n\n第二段同标签无标记。\n\n想一下范围。"
    assert "天气" not in out and "闲聊" not in out and "**[" not in out and "纠正" not in out and "<!--" not in out


def test_enhanced_preferred(data, stub, tmp_path, ctx_ws):
    mk_enh(ctx_ws)
    rec = rec_for(data)
    assert T.export_name(data, rec) == "2026-10-16_1050_aha.md"
    assert T.process(data, rec, [], log=lambda *_: None, now=NOW)
    wait_status(data, lambda r: r[0]["status"] != "running")
    p = prompts(tmp_path)[0]
    assert "给 dispatch 加个重试" in p and "给低八" not in p and "天气" not in p and "已校对稿" in p


def test_deferred_when_missing_or_stale_then_fallback(data, stub, tmp_path, ctx_ws, monkeypatch):
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    triggered = []
    monkeypatch.setattr(T, "trigger_enhance", lambda src, root: triggered.append(src.name))
    mk_enh(ctx_ws, enh=None)                                   # source exists, enhanced missing
    rec = rec_for(data, ready_s_ago=60)
    assert not T.process(data, rec, [], log=lambda *_: None, now=NOW)
    assert triggered == ["2026-10-16_1050_aha.md"] and T.read_runs(data) == []
    mk_enh(ctx_ws, enh_newer=False)                            # enhanced older than source -> still deferred
    assert not T.process(data, rec, [], log=lambda *_: None, now=NOW)
    assert len(triggered) == 2 and T.read_runs(data) == []
    # after the wait: raw + glossary fallback, version noted
    rec = rec_for(data, ready_s_ago=601)
    monkeypatch.setattr(T, "glossary_text", lambda t: t.replace("低八", "dispatch"))
    assert T.process(data, rec, [], log=lambda *_: None, now=NOW)
    wait_status(data, lambda r: r[0]["status"] != "running")
    p = prompts(tmp_path)[0]
    assert "给dispatch加个重试" in p.replace(" ", "") and "词表正则替换" in p and "已校对稿" not in p


def test_fallback_without_glossary_notes_raw(data, stub, tmp_path, monkeypatch):
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    monkeypatch.setattr(T, "glossary_text", lambda t: None)
    rec = rec_for(data, ready_s_ago=700)                       # no daily_records file at all
    assert T.process(data, rec, [], log=lambda *_: None, now=NOW)
    wait_status(data, lambda r: r[0]["status"] != "running")
    assert "原始 Whisper 转写（校对稿超时" in prompts(tmp_path)[0]


def test_reply_uses_enhanced_and_defers(data, stub, tmp_path, ctx_ws, monkeypatch):
    T.append_run({"uuid": "u-prev", "pid": None, "recording_id": "old", "started": "x", "status": "done",
                  "exit_code": 0, "log_path": str(tmp_path / "l"), "kind": "new"}, data)
    mk_enh(ctx_ws, enh="**[task]** 回复上一个，把范围缩小一点。\n\n**[smalltalk]** 哈哈。\n\n## 纠正\n\n- 无\n",
           src_text="回复上一个 把范围缩小一点")
    rec = rec_for(data, text="回复上一个 把范围缩小一点")
    assert T.process(data, rec, T.read_runs(data), log=lambda *_: None, now=NOW)
    runs = wait_status(data, lambda r: len(r) == 2 and r[1]["status"] != "running")
    p = prompts(tmp_path)[0]
    assert "--resume u-prev" in p and "把范围缩小一点。" in p and "已校对稿" in p and "哈哈" not in p and "回复上一个" not in p
    # deferral for replies too
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    (ctx_ws / "contexts/daily_records/enhanced/2026-10-16_1050_aha.enhance.md").unlink()
    rec = rec_for(data, text="回复上一个 把范围缩小一点", ready_s_ago=10)
    assert not T.process(data, rec, T.read_runs(data), log=lambda *_: None, now=NOW)


def test_user_edited_text_top_priority_no_wait(data, stub, tmp_path, ctx_ws, monkeypatch):
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    mk_enh(ctx_ws)                                             # enhanced exists, still loses
    rec = rec_for(data, ready_s_ago=0)
    T.write_user_text(data, "q1", "手机上改过的版本")
    assert T.resolve_text(data, rec, NOW, log=lambda *_: None) == ("手机上改过的版本", T.V_USER)
    assert T.process(data, rec, [], log=lambda *_: None, now=NOW)
    wait_status(data, lambda r: r[0]["status"] != "running")
    p = prompts(tmp_path)[0]
    assert "[文字版本：用户在手机上确认/修改过]" in p and "手机上改过的版本" in p and "给 dispatch" not in p


def test_user_edited_text_missing_enhance_no_defer(data, monkeypatch):
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    rec = rec_for(data, ready_s_ago=0)
    assert T.resolve_text(data, rec, NOW, log=lambda *_: None) is None   # control: defers without edited text
    T.write_user_text(data, "q1", "x")
    assert T.resolve_text(data, rec, NOW, log=lambda *_: None) == ("x", T.V_USER)


def test_claude_bin_env_wins_then_local_bin_before_path(tmp_path, monkeypatch):
    import aha_common as C
    home, other = tmp_path / "home", tmp_path / "brew"
    for d, tag in ((home / ".local" / "bin", "native"), (other, "old")):
        d.mkdir(parents=True)
        (d / "claude").write_text(f"#!/bin/sh\necho {tag}\n")
        (d / "claude").chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{other}:/usr/bin:/bin")        # launchd-style PATH without ~/.local/bin
    monkeypatch.delenv("AHA_CLAUDE_BIN", raising=False)
    assert C.claude_bin() == str(home / ".local" / "bin" / "claude")
    monkeypatch.setenv("AHA_CLAUDE_BIN", "/x/claude")
    assert C.claude_bin() == "/x/claude"


def test_settings_read_only_access_to_data_dir(data, tmp_path):
    cfg = json.loads(T.build_settings(tmp_path / "s.json").read_text())
    d = str(data)
    allow, deny = cfg["permissions"]["allow"], cfg["permissions"]["deny"]
    assert {f"Read(/{d}/**)", f"Glob(/{d}/**)", f"Grep(/{d}/**)", f"Bash(cat {d}/:*)", f"Bash(wc {d}/:*)"} <= set(allow)
    assert {f"Write(/{d}/**)", f"Edit(/{d}/**)"} <= set(deny)
    assert not any(a.startswith(("Write(", "Edit(")) for a in allow if d in a)   # read-only: no write rule for the data dir
    assert "Bash(kubectl:*)" in deny and "Bash(launchctl:*)" in deny              # existing denies stay
