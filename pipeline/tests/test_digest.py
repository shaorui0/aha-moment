import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pipeline_helpers import make_item
import daily_digest as D

DAY = "2026-10-09"


def seed(root):
    make_item(root, DAY, "amb00001", "09:00:00", "hello one")
    make_item(root, DAY, "amb00002", "09:01:00", "hello two")          # contiguous -> merged
    make_item(root, DAY, "amb00003", "09:30:00", "much later")         # gap -> new block
    make_item(root, DAY, "qck00001", "09:01:30", "quick thought", source="quick", dur=3000)
    make_item(root, DAY, "sil00001", "10:00:00", None, status="silent")
    make_item(root, DAY, "bad00001", "10:01:00", None, status="failed")
    make_item(root, DAY, "pen00001", "10:02:00", None, status="pending")
    make_item(root, "2026-10-08", "oth00001", "09:00:00", "other day")


def test_digest_blocks_and_skips(data):
    seed(data)
    assert D.main(["--date", DAY]) == 0
    t = (data / "digest" / f"{DAY}.md").read_text()
    assert "09:00–09:02 · ambient, 2 segments" in t
    assert "hello one\nhello two" in t
    assert "### 09:01 · quick" in t and "quick thought" in t
    assert "much later" in t and "other day" not in t
    assert t.index("quick thought") < t.index("much later")
    assert "silent=1" in t and "failed=1" in t and "pending=1" in t
    assert "file://" in t and "AI-derived" not in t


def test_quick_not_merged_into_ambient(data):
    seed(data)
    items, _ = D.C.load_items(data, DAY)
    blocks = D.C.build_blocks(items)
    # time order: amb1, amb2, quick(09:01:30), amb3
    assert [(b.source, len(b.items)) for b in blocks] == [("ambient", 2), ("quick", 1), ("ambient", 1)]


def test_idempotent(data):
    seed(data)
    D.main(["--date", DAY]); a = (data / "digest" / f"{DAY}.md").read_text()
    D.main(["--date", DAY]); b = (data / "digest" / f"{DAY}.md").read_text()
    assert a == b
    assert sorted(p.name for p in (data / "digest").iterdir()) == [f"{DAY}.md"]


def test_empty_day(data):
    assert D.main(["--date", DAY]) == 0
    assert "no usable transcripts" in (data / "digest" / f"{DAY}.md").read_text()


def test_bad_date(data):
    assert D.main(["--date", "nope"]) == 2


def test_llm_section_separate(data, fake_claude):
    seed(data)
    D.main(["--date", DAY])
    raw = (data / "digest" / f"{DAY}.md").read_text()
    D.main(["--date", DAY, "--llm"])
    t = (data / "digest" / f"{DAY}.md").read_text()
    assert t.startswith(raw.rstrip("\n"))          # raw timeline untouched
    assert "NOT verbatim" in t and "STUB-ANSWER" in t and "--tools" in t


def test_llm_failure_keeps_raw(data, fake_claude, monkeypatch):
    seed(data)
    monkeypatch.setenv("FAKE_RC", "3")
    assert D.main(["--date", DAY, "--llm"]) == 0
    t = (data / "digest" / f"{DAY}.md").read_text()
    assert "hello one" in t and "LLM step failed" in t
