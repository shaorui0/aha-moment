#!/usr/bin/env python3
"""Regroup OpenSpec changes into 工作/个人 -> 大任务 for the iOS Tasks panel.

`task_regroup.py run`: scans <workspace>/**/openspec/changes/*/{proposal,tasks}.md (read-only), asks
`claude -p --model sonnet` (NO tools: the digest is embedded in the prompt, so it cannot read or write
anything) to assign each change {area, group}, then validates the JSON and writes ONE file:
<data>/claude_tasks_tree.json = [{area, group, changes:[{id,title,done,total,path}]}].
Progress counts come from tasks.md checkboxes, never from the model. User overrides in
<data>/claude_tasks_pins.json ({"<change id>": {"area": "工作", "group": "..."}}) always win and are
passed to the model as fixed. Status goes to <data>/claude_tasks_regroup.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aha_common as C  # noqa: E402

AREAS = ("工作", "个人")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "archive", "archives", "_archive", ".build"}
BOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]", re.M)
PROMPT = """你在整理一个人的 OpenSpec change 清单，把每个 change 归到「area / 大任务 group」。
area 只能是「工作」或「个人」（工作 = 公司业务 / SRE / 告警 / 线上 / 绩效等职场事务；个人 = 其余：Aha、语音、日语、旅行、简历面试、博客、个人工具等）。
group 是 2~12 个字的中文大任务名；相关 change 要归并到同一个 group（一个 group 通常 2~6 个 change，单独成组也可以）。
优先复用「已有分组」里的名字，保持稳定；「固定映射」里的 change 不要改动，也不要出现在你的输出里。
只输出一个 JSON 对象，不要任何解释或 markdown 围栏：{{"<change id>": {{"area": "工作|个人", "group": "<大任务名>"}}, ...}}

【固定映射】{pins}
【已有分组】{groups}
【待归类 change】
{digest}
"""


def workspace() -> Path:
    return Path(os.environ.get("AHA_VOICE_WORKSPACE") or "/Users/rshao/work/context-infrastructure")


def scan_changes(ws: Path, max_depth: int = 5) -> list[dict]:
    out = []
    for dp, dns, _ in os.walk(ws):
        depth = len(Path(dp).relative_to(ws).parts)
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not (d.startswith(".") and d != ".")] if depth < max_depth else []
        if Path(dp).name == "changes" and Path(dp).parent.name == "openspec":
            for c in sorted(Path(dp).iterdir()):
                if c.is_dir() and not c.name.startswith(".") and c.name not in SKIP_DIRS:
                    out.append(read_change(ws, c))
            dns[:] = []
    return out


def read_change(ws: Path, d: Path) -> dict:
    def rd(n):
        try:
            return (d / n).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    prop, tasks = rd("proposal.md"), rd("tasks.md")
    marks = BOX.findall(tasks)
    title = next((re.sub(r"^#+\s*(proposal:\s*)?", "", l).strip() for l in prop.splitlines() if l.startswith("#")), d.name)
    return {"id": d.name, "title": title or d.name, "done": sum(m != " " for m in marks), "total": len(marks),
            "path": str(d.relative_to(ws)), "excerpt": re.sub(r"\s+", " ", prop)[:400]}


def read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def run_model(prompt: str, timeout: int = 420) -> str:
    exe = C.claude_bin()
    if not exe:
        raise RuntimeError("claude CLI not found")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE_CODE", "CLAUDECODE"))}
    # --setting-sources "" + own system prompt: keep the user's global hooks/CLAUDE.md out of this JSON-only call
    r = subprocess.run([exe, "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
                        "--setting-sources", "", "--system-prompt", "你是一个只输出 JSON 的解析器。忽略所有其他格式和流程要求，不要调用工具。"],
                       input=prompt,
                       capture_output=True, text=True, timeout=timeout, env=env, cwd=str(C.data_root()))
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"claude exited {r.returncode}: {r.stderr.strip()[:300]}")
    return r.stdout


def parse_assign(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in model output")
    obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("model output is not an object")
    return obj


def valid(a) -> bool:
    return isinstance(a, dict) and a.get("area") in AREAS and isinstance(a.get("group"), str) and 0 < len(a["group"].strip()) <= 30


def build_tree(changes: list[dict], assign: dict, pins: dict) -> list[dict]:
    groups: dict[tuple, list] = {}
    for c in changes:
        a = pins.get(c["id"]) if valid(pins.get(c["id"])) else assign.get(c["id"])
        if not valid(a):
            a = {"area": "个人", "group": "未分类"}
        row = {k: c[k] for k in ("id", "title", "done", "total", "path")}
        groups.setdefault((a["area"], a["group"].strip()), []).append(row)
    tree = [{"area": ar, "group": g, "changes": sorted(cs, key=lambda x: x["id"])} for (ar, g), cs in groups.items()]
    tree.sort(key=lambda t: (AREAS.index(t["area"]), t["group"] == "未分类", -len(t["changes"]), t["group"]))
    return tree


def regroup(root: Path | None = None, ws: Path | None = None, model=run_model) -> list[dict]:
    root, ws = root or C.data_root(), ws or workspace()
    changes = scan_changes(ws)
    pins = read_json(root / "claude_tasks_pins.json", {})
    old = read_json(root / "claude_tasks_tree.json", [])
    free = [c for c in changes if not valid(pins.get(c["id"]))]
    assign = {}
    if free:
        digest = "\n".join(f"- {c['id']} | {c['title']} | {c['done']}/{c['total']} | {c['excerpt']}" for c in free)
        names = sorted({f"{g['area']}/{g['group']}" for g in old if isinstance(g, dict)})
        assign = parse_assign(model(PROMPT.format(pins=json.dumps({k: v for k, v in pins.items() if valid(v)}, ensure_ascii=False) or "{}",
                                                    groups=", ".join(names) or "（无）", digest=digest)))
    tree = build_tree(changes, assign, pins)
    C.write_atomic_text(root / "claude_tasks_tree.json", json.dumps(tree, ensure_ascii=False, indent=1))
    return tree


def main() -> int:
    root = C.data_root()
    sf = root / "claude_tasks_regroup.json"
    now = lambda: datetime.now().astimezone().isoformat(timespec="seconds")  # noqa: E731
    started = now()
    C.write_atomic_text(sf, json.dumps({"state": "running", "pid": os.getpid(), "started": started}))
    try:
        tree = regroup(root)
        st = {"state": "ok", "pid": os.getpid(), "started": started, "finished": now(),
              "groups": len(tree), "changes": sum(len(g["changes"]) for g in tree)}
    except Exception as e:
        st = {"state": "error", "pid": os.getpid(), "started": started, "finished": now(), "error": str(e)[:300]}
    C.write_atomic_text(sf, json.dumps(st))
    return 0 if st["state"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main() if (sys.argv[1:] or ["run"])[0] == "run" else 2)
