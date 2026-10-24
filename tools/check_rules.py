#!/usr/bin/env python3
"""Self-check for the docs layer and public safety of this repository.

Checks:
  1. required docs exist (METHOD, REQUIREMENTS)
  2. backticked repo paths in docs/METHOD.md and docs/REQUIREMENTS.md exist
  3. no leak patterns in tracked-or-new text files
       public: AWS account id, AWS access key id
       private: one regex per line in `.leak_patterns.local` (gitignored, so the repo never
       contains the words it guards against)
  4. no home-directory paths in the method layer: docs/METHOD|REQUIREMENTS|PROCESS.md, openspec/,
       reports/, notes/, tools/, .claude/ (launchd plists, older docs and code keep theirs)
  5. no audio or transcript files in the tree (they belong in ~/aha_data);
       asr/samples/ holds synthetic TTS benchmark clips and is allowed

Exit 0 = pass, 1 = fail. Usage: python3 tools/check_rules.py [repo_root]
"""
import re
import subprocess
import sys
from pathlib import Path

REQUIRED = ["docs/METHOD.md", "docs/REQUIREMENTS.md", "README.md", "PLAN.md"]
REF_FILES = ["docs/METHOD.md", "docs/REQUIREMENTS.md"]
PUBLIC_LEAKS = [
    r"(?<![\w.-])\d{12}(?![\w.])",  # AWS account id (all-same-digit placeholders are allowed)
    r"AKIA[0-9A-Z]{16}",           # AWS access key id
]
HOME_PATH = re.compile(r"/Users/[A-Za-z0-9_.-]+/")
STRICT_DIRS = ("docs/METHOD.md", "docs/REQUIREMENTS.md", "docs/PROCESS.md", "openspec/", "reports/", "notes/", "tools/", ".claude/")
FORBIDDEN_NAMES = re.compile(r"(^|/)(audio\.(aac|m4a|wav|caf)|transcript\.[^/]*\.(txt|json))$")
FORBIDDEN_EXT = {".aac", ".m4a", ".wav", ".caf", ".mp3"}
SYNTHETIC_AUDIO_DIRS = ("asr/samples/",)   # TTS-generated ASR benchmark clips, not recordings
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
TEXT_EXT = {".md", ".py", ".json", ".yaml", ".yml", ".html", ".txt", ".sh", ".toml", ".plist", ".swift", ""}
PATH_RE = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./<>-]*)`")
DOC_PATH_PREFIXES = ("docs/", "pipeline/", "server/", "asr/", "tools/", "e2e/", "ops/", "ios/", "mac/", "openspec/", "reports/", "notes/", ".claude/")


def leak_patterns(root):
    pats = list(PUBLIC_LEAKS)
    local = root / ".leak_patterns.local"
    if local.exists():
        pats += [l.strip() for l in local.read_text().splitlines() if l.strip() and not l.startswith("#")]
    return [re.compile(p, re.IGNORECASE) for p in pats]


def repo_files(root):
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.split("\n")
        return [root / p for p in out if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p for p in root.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.parts)]


def check(root):
    errors = []
    for rel in REQUIRED:
        if not (root / rel).exists():
            errors.append(f"missing required file: {rel}")

    for rel in REF_FILES:
        f = root / rel
        if not f.exists():
            continue
        for ref in PATH_RE.findall(f.read_text()):
            if not ref.startswith(DOC_PATH_PREFIXES) or "<" in ref or "*" in ref:
                continue
            if not (root / ref.rstrip("/")).exists():
                errors.append(f"{rel}: dangling reference `{ref}`")

    pats = leak_patterns(root)
    for f in repo_files(root):
        if not f.is_file() or f.name == ".leak_patterns.local":
            continue
        rel = str(f.relative_to(root))
        if (f.suffix in FORBIDDEN_EXT and not rel.startswith(SYNTHETIC_AUDIO_DIRS)) or FORBIDDEN_NAMES.search(rel):
            errors.append(f"audio or transcript file in tree: {rel}")
            continue
        if f.suffix not in TEXT_EXT:
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        for p in pats:
            m = next((m for m in p.finditer(text) if len(set(m.group())) > 1), None)
            if m:
                errors.append(f"leak pattern in {rel}:{text.count(chr(10), 0, m.start()) + 1}")
        if rel.startswith(STRICT_DIRS) and rel != "tools/check_rules.py":
            m = HOME_PATH.search(text)
            if m:
                errors.append(f"home path in {rel}:{text.count(chr(10), 0, m.start()) + 1}")
    return errors


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
    errors = check(root)
    for e in errors:
        print(f"FAIL {e}")
    if not errors:
        print("check_rules: OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
