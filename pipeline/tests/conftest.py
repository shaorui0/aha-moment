import json, os, stat, sys, time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("AHA_DATA_DIR", str(tmp_path / "aha"))
    monkeypatch.setenv("TZ", "UTC"); time.tzset()   # HH:MM rendering is local-time
    return tmp_path / "aha"


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A stub `claude` that echoes a marker + the last line of stdin; exit code via FAKE_RC."""
    p = tmp_path / "claude"
    p.write_text('#!/bin/sh\n[ -n "$FAKE_RC" ] && { echo boom >&2; exit "$FAKE_RC"; }\n'
                 'echo "STUB-ANSWER args=$*"\ncat >/dev/null\n')
    p.chmod(0o755)
    monkeypatch.setenv("AHA_CLAUDE_BIN", str(p))
    return p
