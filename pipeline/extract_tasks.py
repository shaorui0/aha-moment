#!/usr/bin/env python3
"""enhanced/*.enhance.md 里的 [task] 段 -> ~/aha_data/inbox/tasks.json（供 GET /v1/tasks 给 iOS 快捷指令拉去提醒事项）。

提取是确定性的；之后用本地 qwen3（经 ollama_gate）把未投递条目的标题概括成一行（目标 ≤14 字，硬上限 20），模型不可用就保留截断标题。
id = sha1(原文文件名 + 段落文字)[:12]；按 id 去重并保留 delivered 标记；
重新 enhance 后文字变了的旧未投递条目标 stale（不删），新文字作为新条目加入。
用法：extract_tasks.py [--all | --date D] [--retitle]   默认 今天+昨天；--retitle 用新提示词重做已概括过的标题
环境变量：AHA_CONTEXT_ROOT、AHA_DATA_DIR
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_WS = HERE.parents[2]
NAME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})(\d{2})(_aha(-\d+)?)\.enhance\.md$")
SEG_RE = re.compile(r"^\*\*\[task\]\*\*\s*(.*)$", re.S)
SENT_END = "。！？!?；;\n"
TITLE_MAX = 40


def tasks_file(data_dir: Path | None = None) -> Path:
    root = data_dir or Path(os.environ.get("AHA_DATA_DIR") or "~/aha_data").expanduser()
    return Path(root) / "inbox" / "tasks.json"


@contextmanager
def locked(path: Path):
    """extract 与服务端 ack 共用的文件锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".tasks.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def load(path: Path) -> list[dict]:
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
        return items if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def save(path: Path, items: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"items": items}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def make_title(text: str) -> str:
    t = text.strip()
    for i, ch in enumerate(t):
        if i > 0 and (ch in SENT_END or (ch in "，," and not (ch == "," and t[i - 1].isdigit() and t[i + 1:i + 2].isdigit()))):
            t = t[:i]   # 句末标点或中/英文逗号都算断句（1,000 这种数字逗号除外）
            break
    t = t.strip(" \t，,、")
    return t if len(t) <= TITLE_MAX else t[:TITLE_MAX - 1] + "…"


LABEL_RE = re.compile(r"^\*\*\[(\w+)\]\*\*\s*(.*)$", re.S)


def task_segments(md: str) -> list[str]:
    """[task] 段；同标签相邻段落只在第一段带标签，后面的续段（无标签）并入同一条（用换行连接）。"""
    body = re.sub(r"<!--.*?-->", "", md.split("## 纠正", 1)[0], flags=re.S)
    out, cur, in_task = [], [], False
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        m = LABEL_RE.match(block)
        if m:
            if cur:
                out.append("\n".join(cur)); cur = []
            in_task = m.group(1) == "task"
            block = m.group(2).strip()
        if in_task and block:
            cur.append(block)
    if cur:
        out.append("\n".join(cur))
    return out


def items_from_file(p: Path) -> list[dict]:
    m = NAME_RE.match(p.name)
    if not m:
        return []
    y, mo, d, h, mi = (int(x) for x in m.groups()[:5])
    created = datetime(y, mo, d, h, mi).isoformat()
    res = []
    raw = p.name[:-len(".enhance.md")] + ".md"   # 原文文件名（id 以它为准，保证文字没变时 id 不变）
    for seg in task_segments(p.read_text(encoding="utf-8")):
        res.append({"id": hashlib.sha1((raw + seg).encode()).hexdigest()[:12],
                    "title": make_title(seg), "note": f"{seg}\n\n— {p.name}",
                    "created": created, "source": p.name, "delivered": False})
    return res


def _recording(source: str) -> str:
    """'X_aha.md' / 'X_aha.enhance.md' -> 'X_aha'（同一条录音，不论新旧命名）。"""
    return source.removesuffix(".md").removesuffix(".enhance")


def run(ddir: Path, days: set[str] | None, out: Path) -> dict:
    new, scanned = [], set()
    for p in sorted((ddir / "enhanced").glob("*_aha*.enhance.md")):
        m = NAME_RE.match(p.name)
        if m and (days is None or "-".join(m.groups()[:3]) in days):
            new += items_from_file(p)
            scanned.add(_recording(p.name))
    with locked(out):
        items = load(out)
        by_id = {i.get("id"): i for i in items}
        newids = {i["id"] for i in new}
        for n in new:                                   # 文字没变：刷新来源名到新文件名，取消 stale
            old = by_id.get(n["id"])
            if old and not old.get("delivered"):
                old["note"], old["source"] = n["note"], n["source"]
                old.pop("stale", None)
        staled = 0
        for i in items:                                 # 同一录音重新 enhance 后文字没了：标 stale，不删
            if (not i.get("delivered") and not i.get("dismissed") and not i.get("stale")
                    and _recording(i.get("source", "")) in scanned and i.get("id") not in newids):
                i["stale"] = True; staled += 1
        known = set(by_id)
        added = [i for i in new if i["id"] not in known and not known.add(i["id"])]
        save(out, items + added)
    return {"scanned_tasks": len(new), "added": len(added), "stale": staled, "total": len(items) + len(added)}


TITLE_TARGET = 14   # 目标汉字数（手机一行）
TITLE_HARD = 20     # 硬上限
TITLE_PROMPT = ("下面是一段语音转写里识别出的待办（口语、可能有错字）。用一句不超过 {n} 个汉字的祈使句概括要做的事，"
                "像提醒事项标题那样，只输出这一句，不要标点结尾、不要引号。\n\n{text}")
BREAKS = "，,、；;：: 　"


def cut_natural(t: str, hard: int = TITLE_HARD) -> str:
    """超长时在 hard 以内最后一个自然断点（标点/空格）处截断；没有断点就硬截到 hard。不加省略号。"""
    if len(t) <= hard:
        return t
    cut = max(t.rfind(c, 0, hard + 1) for c in BREAKS)
    return t[:cut if cut >= 6 else hard].strip(BREAKS)


def _ask_title(gate, text: str, n: int) -> str:
    r = gate.chat("qwen3:14b", [{"role": "user", "content": TITLE_PROMPT.format(n=n, text=text)}],
                  {"temperature": 0.2, "num_predict": 60})
    return gate.content(r).strip().splitlines()[0].strip(" \"'“”「」。.")


def llm_title(gate, text: str) -> str:
    t = _ask_title(gate, text, TITLE_TARGET)
    if len(t) > TITLE_HARD:                        # 超过硬上限：要求更短，重试一次
        t = _ask_title(gate, text, 10) or t
    return cut_natural(t)


def summarize_titles(out: Path, limit: int = 200, force: bool = False) -> int:
    """未投递、标题还没被本地模型概括过的条目 -> 用本地 qwen3（经 ollama_gate）重写成一行标题。
    force=True 只重做「已被概括过」的（换了提示词后刷新旧标题）。模型不可用就保持原标题，下次再试；不阻塞上游。"""
    sys.path.insert(0, os.environ.get("OLLAMA_GATE_DIR", str(HERE.parents[2] / "tools")))
    import ollama_gate as gate  # noqa: E402
    with locked(out):
        todo = [dict(i) for i in load(out)
                if not i.get("delivered") and not i.get("dismissed") and not i.get("stale")
                and ((i.get("title_by") == "llm") if force else (i.get("title_by") != "llm"))][:limit]
    done = {}
    for it in todo:
        text = it["note"].split("\n\n— ", 1)[0][:1500]
        try:
            t = llm_title(gate, text)
        except Exception as e:  # noqa: BLE001
            print(f"[extract_tasks] title llm unavailable: {e}", flush=True)
            break
        if t:
            done[it["id"]] = t
    if done:
        with locked(out):
            items = load(out)
            for i in items:
                if i.get("id") in done and not i.get("delivered"):
                    i["title"], i["title_by"] = done[i["id"]], "llm"
            save(out, items)
    return len(done)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true")
    g.add_argument("--date")
    ap.add_argument("--retitle", action="store_true", help="重做已概括过的标题")
    a = ap.parse_args(argv)
    ddir = Path(os.environ.get("AHA_CONTEXT_ROOT") or DEFAULT_WS) / "contexts" / "daily_records"
    if a.all:
        days = None
    elif a.date:
        days = {a.date}
    else:
        now = datetime.now()
        days = {now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")}
    sys.path.insert(0, str(HERE))
    import tasks_store
    if tasks_file().exists() and tasks_store.is_v2(tasks_file()):   # v2 is owned by synthesize_tasks.py
        print("[extract_tasks] tasks.json is v2 (see synthesize_tasks.py); nothing to do", flush=True)
        return 0
    print(f"[extract_tasks] {run(ddir, days, tasks_file())}", flush=True)
    if not os.environ.get("AHA_TASK_TITLE_NO_LLM"):
        print(f"[extract_tasks] titles summarized: {summarize_titles(tasks_file(), force=a.retitle)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 — 不阻塞上游流水线
        print(f"[extract_tasks] failed: {e}", flush=True)
        sys.exit(0)
