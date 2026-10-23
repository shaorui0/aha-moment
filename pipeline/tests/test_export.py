import json
import time

import pytest

from _pipeline_helpers import make_item
import export_to_context as E

DAY = "2026-10-09"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC"); time.tzset()
    yield
    monkeypatch.undo(); time.tzset()


def with_session(d, sid, start):
    m = json.loads((d / "meta.json").read_text())
    m.update(session_id=sid, session_start=start)
    (d / "meta.json").write_text(json.dumps(m))


def seed(root):
    for iid, t, txt in [("s1c00001", "09:00:00", "first part"), ("s1c00002", "09:01:00", "second part")]:
        with_session(make_item(root, DAY, iid, t, txt), "S1", f"{DAY}T09:00:00+00:00")
    with_session(make_item(root, DAY, "s2c00001", "09:00:40", "same minute other rec"), "S2", f"{DAY}T09:00:40+00:00")
    with_session(make_item(root, DAY, "s3c00001", "10:00:00", None, status="silent"), "S3", f"{DAY}T10:00:00+00:00")
    make_item(root, DAY, "leg00001", "11:00:00", "legacy a")        # no session: contiguous -> one recording
    make_item(root, DAY, "leg00002", "11:01:00", "legacy b")


def test_one_recording_one_file(data, tmp_path, monkeypatch):
    seed(data)
    ws = tmp_path / "ws"; monkeypatch.setenv("AHA_CONTEXT_ROOT", str(ws))
    assert E.main(["--date", DAY]) == 0 and not (ws / "contexts").exists()      # dry-run
    assert E.main(["--date", DAY, "--apply"]) == 0
    dd = ws / "contexts" / "daily_records"
    names = sorted(p.name for p in dd.iterdir())
    assert names == [f"{DAY}_0900_aha-2.md", f"{DAY}_0900_aha.md", f"{DAY}_1100_aha.md"]
    assert (dd / f"{DAY}_0900_aha.md").read_text() == "first part\nsecond part\n"     # text only, chunks in order
    assert (dd / f"{DAY}_0900_aha-2.md").read_text() == "same minute other rec\n"
    assert (dd / f"{DAY}_1100_aha.md").read_text() == "legacy a\nlegacy b\n"         # silent S3 -> no file


def test_stale_removed_and_foreign_kept(data, tmp_path, monkeypatch):
    seed(data)
    ws = tmp_path / "ws"; monkeypatch.setenv("AHA_CONTEXT_ROOT", str(ws))
    dd = ws / "contexts" / "daily_records"; dd.mkdir(parents=True)
    (dd / f"{DAY}_0700_aha.md").write_text("old")            # ours, no longer a recording -> removed
    (dd / f"{DAY}_0700_notes.md").write_text("keep")         # someone else's daily record -> untouched
    E.main(["--date", DAY, "--apply"])
    assert not (dd / f"{DAY}_0700_aha.md").exists() and (dd / f"{DAY}_0700_notes.md").read_text() == "keep"
