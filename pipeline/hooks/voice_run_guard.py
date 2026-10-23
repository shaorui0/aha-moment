#!/usr/bin/env python3
"""PreToolUse guard for voice-triggered Claude runs (active only when AHA_VOICE_RUN=1).

Input: hook JSON on stdin ({tool_name, tool_input, cwd, ...}). Exit 0 = allow (normal permission
flow still applies), exit 2 = hard block (stderr is shown to Claude). Fail-closed on parse errors.

Blocks:
  * Bash that mentions kubectl / helm / aws / kubectl aliases (k, ... + aliases found in
    ~/.zshrc and the workspace dotfiles) / sudo / launchctl / git push / eval / base64 -d.
  * Bash that has a write verb (rm, mv, cp, tee, >, sed -i, ...) together with an absolute or
    ".." path outside the workspace.
  * Write/Edit/MultiEdit/NotebookEdit outside the workspace, or inside .git/ and .claude/.
  * Every mcp__* tool (external side effects).
This is defence in depth next to the --settings deny rules; it is NOT a sandbox (see README).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

WORKSPACE = os.path.realpath(os.environ.get("AHA_VOICE_WORKSPACE") or "/Users/rshao/work/context-infrastructure")

BLOCK_WORDS = {"kubectl", "kubectx", "kubens", "helm", "aws", "k", "kx", "kn", "k3ctl", "k9s", "kubecolor",
               "sudo", "launchctl", "eval"}
WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
WRITE_VERBS = {"rm", "mv", "cp", "tee", "dd", "chmod", "chown", "ln", "touch", "mkdir", "rmdir", "truncate",
               "install", "rsync", "scp", "wget", "unlink", "shred"}
SAFE_DEVICES = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"}
SPLIT = re.compile(r"[\s;&|()`$<>\"'={}\\,]+")


def dynamic_aliases() -> set[str]:
    out: set[str] = set()
    cands = [Path("~/.zshrc").expanduser(), *Path(WORKSPACE, "projects/mac_env/dotfiles").glob("*")]
    for p in cands:
        try:
            for m in re.finditer(r"^\s*alias\s+(\w+)=[\"']?[^\n]*?\b(?:kubectl|helm|aws)\b", p.read_text(errors="ignore"), re.M):
                out.add(m.group(1))
        except OSError:
            pass
    return out


def inside(path: str, base: str = WORKSPACE) -> bool:
    return path == base or path.startswith(base + os.sep)


def resolve(p: str, cwd: str) -> str:
    p = os.path.expanduser(p.replace("$HOME", "~"))
    if not os.path.isabs(p):
        p = os.path.join(cwd, p)
    return os.path.realpath(p)


def check_bash(cmd: str, cwd: str) -> str | None:
    cmd = cmd.replace("${HOME}", "~").replace("$HOME", "~")
    toks = [t for t in SPLIT.split(cmd) if t]
    aliases = dynamic_aliases()
    for t in toks:
        base = t.rsplit("/", 1)[-1]
        if base in BLOCK_WORDS or base in aliases:
            return f"command '{base}' is forbidden in voice-triggered runs (kubectl/helm/aws/aliases/sudo)"
    low = " ".join(toks).lower()
    if re.search(r"\bgit\s+push\b", low):
        return "git push is forbidden in voice-triggered runs"
    if "base64" in toks and any(t in ("-d", "-D", "--decode") for t in toks):
        return "base64 decode is forbidden (obfuscation guard)"
    has_write = (any(t.rsplit("/", 1)[-1] in WRITE_VERBS for t in toks)
                 or re.search(r"(^|[^<>=0-9&])>{1,2}(?!&)", cmd) is not None
                 or re.search(r"\b(sed|perl)\b[^|;&]*\s-[a-zA-Z]*i", cmd) is not None
                 or re.search(r"\bcurl\b[^|;&]*\s-[a-zA-Z]*[oO]\b", cmd) is not None)
    if has_write:
        for t in toks:
            if t in SAFE_DEVICES:
                continue
            if t.startswith(("/", "~")) or t.startswith("$HOME"):
                if not inside(resolve(t, cwd)):
                    return f"write-capable command touches path outside workspace: {t}"
            elif ".." in t.split("/"):
                if not inside(resolve(t, cwd)):
                    return f"write-capable command touches path outside workspace: {t}"
        # redirect targets with no whitespace, e.g. >/tmp/x
        for m in re.finditer(r">{1,2}\s*([^\s;&|)]+)", cmd):
            t = m.group(1)
            if t not in SAFE_DEVICES and not t.startswith("&") and not inside(resolve(t.strip("\"'"), cwd)):
                return f"redirect target outside workspace: {t}"
    return None


def check(data: dict) -> str | None:
    name = data.get("tool_name") or ""
    ti = data.get("tool_input") or {}
    cwd = data.get("cwd") or WORKSPACE
    if name.startswith("mcp__"):
        return "MCP tools are forbidden in voice-triggered runs"
    if name == "Bash":
        return check_bash(str(ti.get("command") or ""), cwd)
    if name in WRITE_TOOLS:
        raw = ti.get(WRITE_TOOLS[name]) or ti.get("file_path") or ""
        if not raw:
            return f"{name} without a path"
        p = resolve(str(raw), cwd)
        if not inside(p):
            return f"{name} outside workspace: {p}"
        rel = os.path.relpath(p, WORKSPACE).split(os.sep)
        if rel[0] in (".git", ".claude"):
            return f"{name} into {rel[0]}/ is forbidden"
    return None


def main() -> int:
    if os.environ.get("AHA_VOICE_RUN") != "1":
        return 0
    try:
        data = json.load(sys.stdin)
        reason = check(data)
    except Exception as e:  # fail closed
        print(f"[voice_run_guard] cannot parse hook input ({e}); blocking", file=sys.stderr)
        return 2
    if reason:
        print(f"[voice_run_guard] BLOCKED: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
