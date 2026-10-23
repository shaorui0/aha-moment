#!/usr/bin/env python3
"""Voice -> non-interactive Claude Code task (#20/#21/#22).

Trigger: recordings whose meta.json has "tags": ["claude"]. One run per recording *session*
(ambient chunks share meta.session_id; quick/legacy items are their own session) once every chunk
of the session has a terminal transcription status and no new chunk arrived for QUIET_S.

  new task : claude -p --session-id <uuid> -- "<wrapped transcript>"          (cwd = workspace)
  reply    : transcript starts with 回复 / 回复上一个 -> claude -p --resume <latest uuid> -- "<text>"
  kill     : `aha claude-kill <uuid|last>` -> SIGTERM, SIGKILL after 10 s

The transcript is passed as ONE argv element (never through a shell, never built by an LLM).
Safety is enforced per invocation (nothing global is touched): a --settings file with deny rules +
a PreToolUse hook (hooks/voice_run_guard.py, active only with AHA_VOICE_RUN=1), --strict-mcp-config.

Runs are recorded in <data>/inbox/claude_runs.jsonl, logs in <data>/claude_runs/<uuid>.log.
A detached supervisor process (`claude_tasks.py supervise <spec>`) waits for claude and records the
exit code, so runs survive the 2-minute dispatch process exiting.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid as uuidlib
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aha_common as C  # noqa: E402

HERE = Path(__file__).resolve().parent
GUARD = HERE / "hooks" / "voice_run_guard.py"
TAG = "claude"
QUIET_S = 150            # newest chunk of a session must be at least this old
TERMINAL = {"done", "silent", "failed"}
MAX_RUNNING = 3
KILL_GRACE_S = 10
ENHANCE_WAIT_S = int(os.environ.get("AHA_ENHANCE_WAIT_S", "600"))   # wait this long (since ready) for the corrected text
ENHANCE_RETRIGGER_S = 300    # don't re-spawn correct_classify for the same file more often than this
REPLY_RE = re.compile(r"^\s*(回复上一个|回复)[\s,:，：、。.;；!！]*")

PROMPT = (
    "下面是我的一段语音转写（可能有同音字/断句错误，请结合上下文理解意图）。\n"
    "例外：如果转写很短（约 40 个汉字 / 25 个英文词以内），说明只是一句问答或闲聊，直接回答，不要开 OpenSpec change。\n"
    "否则按 OpenSpec 流程处理（参考 harness-openspec skill）：在 openspec/changes/ 下写 proposal，"
    "写完 proposal 就停止——不要 apply、不要实施、不要改 proposal 以外的代码或配置。\n"
    "硬约束：只允许修改本 workspace 内的文件（~/aha_data 只能读，不能写）；不得运行 kubectl（含 k 等集群别名）、helm、aws。\n\n"
    "--- 语音转写开始 ---\n{text}\n--- 语音转写结束 ---"
)

ALLOW = ["Read", "Glob", "Grep", "Write", "Edit", "Bash(openspec:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(rg:*)",
         "Bash(grep:*)", "Bash(find:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(date:*)",
         "Bash(mkdir:*)", "Bash(pwd)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)"]
READ_CMDS = ("cat", "grep", "ls", "head", "tail", "wc")


def data_read_allow(data_dir: Path | str) -> list[str]:
    """Read-only access to the Aha data dir (transcripts, tasks.json, run logs), which is outside the workspace and so
    not covered by the plain Read/Glob/Grep rules. Permission paths use '//' for an absolute path. Writes stay denied:
    Write/Edit are not allowed here and hooks/voice_run_guard.py blocks writes outside the workspace."""
    d = str(Path(data_dir).expanduser())
    return ([f"{t}(/{d}/**)" for t in ("Read", "Glob", "Grep")]
            + [f"Bash({c} {d}/:*)" for c in READ_CMDS])


def data_write_deny(data_dir: Path | str) -> list[str]:
    d = str(Path(data_dir).expanduser())
    return [f"{t}(/{d}/**)" for t in ("Write", "Edit", "MultiEdit", "NotebookEdit")]


def shell_k8s_aliases() -> list[str]:
    """Aliases in ~/.zshrc / workspace dotfiles that wrap kubectl/helm/aws (cluster names stay out of the repo)."""
    out: set[str] = set()
    for p in [Path("~/.zshrc").expanduser(), *Path(workspace(), "projects/mac_env/dotfiles").glob("*")]:
        try:
            out.update(re.findall(r"^\s*alias\s+(\w+)=[\"']?[^\n]*?\b(?:kubectl|helm|aws)\b", p.read_text(errors="ignore"), re.M))
        except OSError:
            pass
    return sorted(out)


DENY_CMDS = ["kubectl", "kubectx", "kubens", "kubecolor", "helm", "aws", "k", "kx", "kn", "k3ctl", "k9s",
             "sudo", "launchctl"]


def deny_cmds() -> list[str]:
    return DENY_CMDS + [a for a in shell_k8s_aliases() if a not in DENY_CMDS]


def workspace() -> str:
    return os.environ.get("AHA_VOICE_WORKSPACE") or "/Users/rshao/work/context-infrastructure"


# ------------------------------------------------------------------ paths / run records
def runs_file(root: Path | None = None) -> Path:
    return (root or C.data_root()) / "inbox" / "claude_runs.jsonl"


def runs_dir(root: Path | None = None) -> Path:
    return (root or C.data_root()) / "claude_runs"


class _Lock:
    def __init__(self, root: Path | None = None):
        p = (root or C.data_root()) / "inbox"
        p.mkdir(parents=True, exist_ok=True)
        self.fh = open(p / ".claude_runs.lock", "w")

    def __enter__(self):
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        self.fh.close()


def read_runs(root: Path | None = None) -> list[dict]:
    f = runs_file(root)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def _write_runs(runs: list[dict], root: Path | None = None) -> None:
    C.write_atomic_text(runs_file(root), "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in runs))


def append_run(rec: dict, root: Path | None = None) -> None:
    with _Lock(root):
        runs = read_runs(root)
        runs.append(rec)
        _write_runs(runs, root)


def update_run(uuid: str, recording_id: str, only_if_status: str | None = None, root: Path | None = None, **fields) -> None:
    with _Lock(root):
        runs = read_runs(root)
        for r in runs:
            if r.get("uuid") == uuid and r.get("recording_id") == recording_id:
                if only_if_status and r.get("status") != only_if_status:
                    return
                r.update(fields)
        _write_runs(runs, root)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reconcile(root: Path | None = None) -> None:
    """A 'running' run whose supervisor is gone (reboot, hard kill) becomes 'lost'."""
    for r in read_runs(root):
        if r.get("status") == "running" and r.get("pid") and not pid_alive(r["pid"]):
            update_run(r["uuid"], r["recording_id"], only_if_status="running", root=root,
                       status="lost", finished=now_iso())


# ------------------------------------------------------------------ selection / grouping
def _parse(s):
    return C._parse_ts(s)


def build_rec(key: str, metas: list[dict], now: datetime) -> dict | None:
    """Session record {recording_id, start, text, ready_at} from time-sorted metas (each with "_dir"); None if no text."""
    texts = []
    for m in metas:
        t = C._transcript_text(m["_dir"], m)
        if m.get("transcription_status") == "done" and t and t.strip():
            texts.append(t.strip())
    if not texts:
        return None
    got = [_parse(m.get("received_at")) or _parse(m.get("recorded_at")) for m in metas]
    got = [g for g in got if g]
    return {"recording_id": key, "start": metas[0].get("recorded_at") or "", "text": "\n".join(texts),
            "ready_at": max(got) if got else now}


def scan_sessions(root: Path, now: datetime, days: int = 2, quiet_s: int = QUIET_S) -> list[dict]:
    """Return ready, tagged sessions: [{recording_id, start, text}], oldest first."""
    sess: dict[str, list[dict]] = {}
    for back in range(days):
        day = (now - timedelta(days=back)).strftime("%Y/%m/%d")
        for mp in sorted((root / day).glob("*/meta.json")):
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
                meta["_dir"] = mp.parent
                key = meta.get("session_id") or meta["id"]
                sess.setdefault(key, []).append(meta)
            except Exception:
                continue
    ready = []
    for key, metas in sess.items():
        if not any(TAG in (m.get("tags") or []) for m in metas):
            continue
        if any(m.get("transcription_status") not in TERMINAL for m in metas):
            continue                                         # a chunk is still being transcribed
        metas.sort(key=lambda m: m.get("recorded_at") or "")
        if metas[0].get("session_id"):                       # ambient session: wait for the tail
            last = max((_parse(m.get("received_at")) or _parse(m.get("recorded_at")) or now) for m in metas)
            if (now - last).total_seconds() < quiet_s:
                continue
        rec = build_rec(key, metas, now)
        if rec:
            ready.append(rec)
    ready.sort(key=lambda s: s["start"])
    return ready


def parse_reply(text: str) -> str | None:
    """Reply body if the transcript starts with 回复 / 回复上一个, else None."""
    m = REPLY_RE.match(text)
    return text[m.end():].strip() if m else None


# ------------------------------------------------------------------ corrected (enhanced) text
V_ENHANCED = "已校对稿（本地模型纠错+分段，已去闲聊段）"
V_GLOSSARY = "原始 Whisper 转写 + 词表正则替换（校对稿超时未生成，未经 LLM 校对，可能有同音字错误）"
V_RAW = "原始 Whisper 转写（校对稿超时未生成，未经任何校对，可能有同音字错误）"
LABEL_RE = re.compile(r"^\*\*\[(task|thinking|diary|smalltalk)\]\*\*\s*", re.I)


def context_root() -> Path:
    import export_to_context as E
    return Path(os.environ.get("AHA_CONTEXT_ROOT") or E.DEFAULT_WS)


def export_name(root: Path, rec: dict) -> str | None:
    """daily_records file name (YYYY-MM-DD_HHMM_aha[-N].md) the export gives this session, or None."""
    import export_to_context as E
    start = _parse(rec.get("start"))
    if not start:
        return None
    day = start.strftime("%Y-%m-%d")
    recs = E.recordings(root, day)
    names = list(E.plan(day, recs))
    for (st, text), name in zip(recs, names):
        if text.strip() == rec["text"].strip():
            return name
    for (st, _), name in zip(recs, names):
        if st.strftime("%H%M") == start.strftime("%H%M"):
            return name
    return None


def parse_enhanced(md: str) -> str:
    """Enhanced file -> plain paragraphs: drop header comment, **[label]** markers, '## 纠正' section, smalltalk.
    Same-label neighbours are merged by render(): only the first paragraph carries the marker, so track the label."""
    md = re.sub(r"\A\s*<!--.*?-->", "", md, flags=re.S)
    md = re.split(r"^## 纠正\s*$", md, maxsplit=1, flags=re.M)[0]
    kept, dropped, label = [], [], None
    for para in re.split(r"\n\s*\n", md.strip()):
        para = para.strip()
        if not para:
            continue
        m = LABEL_RE.match(para)
        if m:
            label, para = m.group(1).lower(), para[m.end():].strip()
        (dropped if label == "smalltalk" else kept).append(para)
    return "\n\n".join(kept or dropped)   # an all-smalltalk recording: keep it rather than send nothing


def trigger_enhance(src: Path, root: Path) -> None:
    """Fire-and-forget correct_classify for ONE file (it takes its own single-instance lock)."""
    marker = root / "inbox" / f".enhance_trigger.{src.name}"
    try:
        if marker.exists() and time.time() - marker.stat().st_mtime < ENHANCE_RETRIGGER_S:
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        subprocess.Popen([sys.executable, str(HERE / "correct_classify.py"), "--file", str(src)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception:
        pass


def glossary_text(text: str) -> str | None:
    try:
        import correct_classify as CC
        hard, _ = CC.load_glossary()
        return CC.apply_glossary(text, hard)[0]
    except Exception:
        return None


V_USER = "用户在手机上确认/修改过"


def user_text_file(root: Path, sid: str) -> Path:
    return root / "inbox" / "claude_text" / f"{sid}.txt"


def read_user_text(root: Path, sid: str) -> str | None:
    """Text the user confirmed/edited on the phone (POST /v1/sessions/<sid>/claude), or None."""
    try:
        t = user_text_file(root, sid).read_text(encoding="utf-8")
    except OSError:
        return None
    return t.strip() or None


def write_user_text(root: Path, sid: str, text: str) -> None:
    C.write_atomic_text(user_text_file(root, sid), text.strip() + "\n")


def enhanced_text(root: Path, rec: dict, dry: bool = False) -> tuple[str | None, str | None]:
    """(enhanced body or None, daily_records file name or None). Missing/stale -> fire-and-forget enhance (unless dry)."""
    ws = context_root()
    name = export_name(root, rec)
    src = ws / "contexts" / "daily_records" / name if name else None
    if src is not None and src.exists():
        enh = src.parent / "enhanced" / (src.name[:-3] + ".enhance.md")
        if enh.exists() and enh.stat().st_mtime >= src.stat().st_mtime:
            body = parse_enhanced(enh.read_text(encoding="utf-8"))
            if body.strip():
                return body, name
        elif not dry:
            trigger_enhance(src, root)
    return None, name


def resolve_text(root: Path, rec: dict, now: datetime, log=print, dry: bool = False) -> tuple[str, str] | None:
    """(text, version note) to send to Claude, or None = defer. Text the user confirmed on the phone wins and never
    waits; otherwise voice text is never sent raw before the correction round had ENHANCE_WAIT_S (since the
    recording became ready) to produce the enhanced file."""
    rid = rec["recording_id"]
    mine = read_user_text(root, rid)
    if mine is not None:
        return mine, V_USER
    body, _ = enhanced_text(root, rec, dry)
    if body is not None:
        return body, V_ENHANCED
    waited = (now - rec["ready_at"]).total_seconds() if rec.get("ready_at") else 0
    if waited < ENHANCE_WAIT_S:
        log(f"{rid}: waiting for enhanced text ({int(waited)}/{ENHANCE_WAIT_S}s), deferred")
        return None
    g = glossary_text(rec["text"])
    log(f"{rid}: enhanced text missing after {int(waited)}s, falling back to {'glossary' if g else 'raw'} text")
    return (g, V_GLOSSARY) if g is not None else (rec["text"], V_RAW)


# ------------------------------------------------------------------ launching
def build_settings(path: Path) -> Path:
    hook = f"AHA_VOICE_RUN=1 {sys.executable} {GUARD}"
    settings = {
        "permissions": {
            "allow": ALLOW + data_read_allow(C.data_root()),
            "deny": ([f"Bash({c}:*)" for c in deny_cmds()] + [f"Bash({c})" for c in deny_cmds()] + ["WebFetch", "WebSearch"]
                     + data_write_deny(C.data_root())),
        },
        "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": hook}]}]},
    }
    C.write_atomic_text(path, json.dumps(settings, indent=2))
    return path


MODEL = "sonnet"  # fixed in v1: no per-task model choice


def claude_argv(exe: str, settings: Path, mode: str, uuid: str, prompt: str, recording_id: str = "") -> list[str]:
    sess = ["--session-id", uuid] if mode == "new" else ["--resume", uuid]
    name = ["--name", f"aha-{recording_id or uuid[:8]}"] if mode == "new" else []  # "aha-" prefix marks voice-triggered sessions
    return [exe, "-p", *sess, *name, "--model", MODEL,
            "--settings", str(settings),
            "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--strict-mcp-config",
            "--disallowedTools", ",".join(["WebFetch", "WebSearch"] + [f"Bash({c}:*)" for c in deny_cmds()]),
            "--", prompt]


def launch(root: Path, mode: str, uuid: str, recording_id: str, prompt: str) -> dict:
    exe = C.claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found")
    rd = runs_dir(root)
    rd.mkdir(parents=True, exist_ok=True)
    log = rd / f"{uuid}.log"
    settings = build_settings(rd / "voice_run_settings.json")
    argv = claude_argv(exe, settings, mode, uuid, prompt, recording_id)
    spec = rd / f".spec-{uuid}-{recording_id}.json"
    spec.write_text(json.dumps({"argv": argv, "cwd": workspace(), "log": str(log), "uuid": uuid,
                                "recording_id": recording_id, "mode": mode, "root": str(root)}))
    rec = {"uuid": uuid, "pid": None, "recording_id": recording_id, "started": now_iso(), "status": "running",
           "exit_code": None, "log_path": str(log), "kind": "reply" if mode == "resume" else mode}
    append_run(rec, root)                                   # record BEFORE spawn: no double start on crash
    p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "supervise", str(spec)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True, env={**os.environ, "AHA_VOICE_RUN": "1"})
    update_run(uuid, recording_id, root=root, pid=p.pid)
    rec["pid"] = p.pid
    return rec


def supervise(spec_path: Path) -> int:
    spec = json.loads(spec_path.read_text())
    root = Path(spec["root"])
    with open(spec["log"], "a", encoding="utf-8") as lg:
        lg.write(f"\n===== {now_iso()} {spec['mode']} recording={spec['recording_id']} =====\n")
        lg.flush()
        env = {**os.environ, "AHA_VOICE_RUN": "1"}
        try:
            rc = subprocess.run(spec["argv"], cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=lg,
                                stderr=subprocess.STDOUT, env=env).returncode
        except Exception as e:
            lg.write(f"supervisor failed to run claude: {e}\n")
            rc = 127
        lg.write(f"===== exit {rc} =====\n")
    update_run(spec["uuid"], spec["recording_id"], only_if_status="running", root=root,
               status="done" if rc == 0 else "failed", exit_code=rc, finished=now_iso())
    try:
        spec_path.unlink()
    except OSError:
        pass
    return rc


# ------------------------------------------------------------------ orchestration
def process(root: Path, rec: dict, runs: list[dict], log=print, dry: bool = False, now: datetime | None = None) -> bool:
    """Handle one ready session. Returns True if something was started/recorded."""
    rid, text = rec["recording_id"], rec["text"]
    reply = parse_reply(text)               # intent is decided on the raw text ...
    running = [r for r in runs if r.get("status") == "running" and pid_alive(r.get("pid"))]
    if len(running) >= MAX_RUNNING:
        log(f"{rid}: {len(running)} runs active, deferred")
        return False
    got = resolve_text(root, rec, (now or datetime.now()).astimezone(), log, dry)   # ... the body comes from the corrected text
    if got is None:
        return False
    text, version = got
    if reply is not None:
        m = REPLY_RE.match(text)
        reply = text[m.end():].strip() if m else (text.strip() if reply else "")
    note = f"[文字版本：{version}]\n"
    if reply is not None:
        prev = [r for r in runs if r.get("kind") in ("new", "reply") and r.get("status") != "skipped"]
        if prev:
            last = prev[-1]
            if last.get("status") == "running" and pid_alive(last.get("pid")):
                log(f"{rid}: reply deferred, run {last['uuid']} still running")
                return False
            if not reply:
                log(f"{rid}: empty reply, skipped")
                if not dry:
                    append_run({"uuid": last["uuid"], "pid": None, "recording_id": rid, "started": now_iso(),
                                "status": "skipped", "exit_code": None, "log_path": last["log_path"], "kind": "skipped"}, root)
                return True
            log(f"{rid}: reply -> resume {last['uuid']}")
            if not dry:
                launch(root, "resume", last["uuid"], rid, note + reply)
            return True
        log(f"{rid}: reply but no previous run; starting a new task")
        text = reply or text
    u = str(uuidlib.uuid4())
    log(f"{rid}: new task {u}")
    if not dry:
        launch(root, "new", u, rid, PROMPT.format(text=note + text))
    return True


def scan(root: Path | None = None, now: datetime | None = None, log=print, dry: bool = False,
         quiet_s: int = QUIET_S) -> int:
    root = root or C.data_root()
    now = (now or datetime.now()).astimezone()
    n = 0
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".claude_scan.lock", "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        reconcile(root)
        done_ids = {r["recording_id"] for r in read_runs(root)}
        for rec in scan_sessions(root, now, quiet_s=quiet_s):
            if rec["recording_id"] in done_ids:
                continue
            try:
                if process(root, rec, read_runs(root), log, dry, now):
                    n += 1
            except Exception as e:
                log(f"{rec['recording_id']}: claude task error: {e}")
    return n


# ------------------------------------------------------------------ kill (#22)
def find_run(which: str, root: Path | None = None) -> dict:
    runs = [r for r in read_runs(root) if r.get("kind") != "skipped"]
    if not runs:
        raise SystemExit("no claude runs recorded")
    if which == "last":
        return runs[-1]
    m = [r for r in runs if r["uuid"].startswith(which)]
    if not m:
        raise SystemExit(f"no run matches {which!r}")
    return m[-1]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):   # macOS: EPERM when only zombies remain in the group
        return False


def kill_run(which: str, root: Path | None = None, grace: float = KILL_GRACE_S, out=print) -> int:
    r = find_run(which, root)
    uuid, rid, pid = r["uuid"], r["recording_id"], r.get("pid")
    if r.get("status") != "running" or not pid_alive(pid):
        out(f"run {uuid} is not running (status={r.get('status')})")
        return 1
    try:
        cmd = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    except Exception:
        cmd = ""
    if "claude_tasks.py" not in cmd:
        out(f"pid {pid} is not a claude_tasks supervisor ({cmd.strip()[:60]!r}); refusing to kill")
        return 1
    pgid = os.getpgid(int(pid))
    os.killpg(pgid, signal.SIGTERM)
    out(f"SIGTERM -> process group {pgid} (run {uuid})")
    deadline = time.time() + grace
    while time.time() < deadline and _group_alive(pgid):
        time.sleep(0.2)
    how = "SIGTERM"
    if _group_alive(pgid):
        os.killpg(pgid, signal.SIGKILL)
        how = "SIGKILL"
        out("still alive after grace; SIGKILL sent")
        time.sleep(0.3)
    update_run(uuid, rid, root=root, status="killed", finished=now_iso(), killed_by=how)
    out(f"run {uuid} killed ({how})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("supervise"); s.add_argument("spec", type=Path)
    k = sub.add_parser("kill"); k.add_argument("which")
    sc = sub.add_parser("scan"); sc.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "supervise":
        return supervise(a.spec)
    if a.cmd == "kill":
        return kill_run(a.which)
    print(f"started {scan(dry=a.dry_run)} claude task(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
