"""Invariants of the export step (docs/METHOD.md I4, I1): one recording -> one text-only file,
same-minute recordings get -2/-3, the data root (source of truth) is never touched, re-running is stable.

Fixtures only: AHA_DATA_DIR and AHA_CONTEXT_ROOT point into tmp_path, ~/aha_data is never read.
"""
import json
import time

import pytest

from _pipeline_helpers import make_item
import aha_common as C
import export_to_context as E

DAY = "2026-10-09"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC"); time.tzset()
    yield
    monkeypatch.undo(); time.tzset()


def session(d, sid, start):
    m = json.loads((d / "meta.json").read_text())
    m.update(session_id=sid, session_start=start)
    (d / "meta.json").write_text(json.dumps(m))


def rec(root, sid, minute, chunks):
    """One recording starting at 09:<minute>:00 with the given chunk texts, one chunk per minute."""
    for i, text in enumerate(chunks):
        iid = f"{sid}c{i:05d}"
        d = make_item(root, DAY, iid, f"09:{minute + i:02d}:00", text)
        session(d, sid, f"{DAY}T09:{minute:02d}:00+00:00")


def tree(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def ws(data, tmp_path, monkeypatch):
    w = tmp_path / "ws"
    monkeypatch.setenv("AHA_CONTEXT_ROOT", str(w))
    assert tmp_path in C.data_root().parents          # isolation: the fixture data root, not ~/aha_data
    return w / "contexts" / "daily_records"


def test_text_only_no_heading_no_metadata(data, ws):
    rec(data, "S1", 0, ["alpha one", "beta two"])
    assert E.main(["--date", DAY, "--apply"]) == 0
    files = list(ws.iterdir())
    assert [p.name for p in files] == [f"{DAY}_0900_aha.md"]
    body = files[0].read_text()
    assert body == "alpha one\nbeta two\n"            # exact: chunks in order, one trailing newline
    assert not body.startswith(("#", "---", "<!--", "["))
    assert "\n\n" not in body and "S1" not in body and DAY not in body   # no blank lines, no id, no date


def test_same_minute_gets_dash_2_and_dash_3_in_start_order(data, ws):
    for sid, sec, text in [("A1", 0, "first"), ("B2", 20, "second"), ("C3", 40, "third")]:
        d = make_item(data, DAY, f"{sid}c00000", f"09:00:{sec:02d}", text)
        session(d, sid, f"{DAY}T09:00:{sec:02d}+00:00")
    E.main(["--date", DAY, "--apply"])
    got = {p.name: p.read_text() for p in ws.iterdir()}
    assert got == {f"{DAY}_0900_aha.md": "first\n", f"{DAY}_0900_aha-2.md": "second\n", f"{DAY}_0900_aha-3.md": "third\n"}


def test_recording_without_text_gets_no_file(data, ws):
    d = make_item(data, DAY, "SILENTc00000", "09:00:00", None, status="silent")
    session(d, "SILENT", f"{DAY}T09:00:00+00:00")
    E.main(["--date", DAY, "--apply"])
    assert not ws.exists() or list(ws.iterdir()) == []


def test_export_never_touches_the_data_root(data, ws):
    rec(data, "S1", 0, ["alpha", "beta"]); rec(data, "S2", 30, ["gamma"])
    before = tree(data)
    E.main(["--date", DAY]); E.main(["--date", DAY, "--apply"])
    assert tree(data) == before                        # audio-side tree is byte-identical (I1)


def test_second_apply_is_a_noop(data, ws, capsys):
    rec(data, "S1", 0, ["alpha", "beta"])
    E.main(["--date", DAY, "--apply"])
    snap = tree(ws.parent.parent)
    capsys.readouterr()
    E.main(["--date", DAY, "--apply"])
    assert "0 new/changed, 0 stale" in capsys.readouterr().out
    assert tree(ws.parent.parent) == snap


def test_recording_that_grows_is_rewritten_not_duplicated(data, ws):
    rec(data, "S1", 0, ["alpha"])
    E.main(["--date", DAY, "--apply"])
    rec(data, "S1", 0, ["alpha", "beta"])              # another chunk of the same recording arrives
    E.main(["--date", DAY, "--apply"])
    assert {p.name: p.read_text() for p in ws.iterdir()} == {f"{DAY}_0900_aha.md": "alpha\nbeta\n"}
