import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compare_ios as ci  # noqa: E402


def mk(tmp, rid, mac_text):
    d = tmp / rid[:4] / rid[4:6] / rid[6:8] / rid
    d.mkdir(parents=True)
    (d / "transcript.x_f2.txt").write_text(mac_text, encoding="utf-8")


def test_cer_matches_bench_definition():
    assert ci.cer("你好，世界", "你好 世界") == 0.0
    assert ci.cer("abcd", "abxd") == 0.25
    assert ci.cer("", "x") == 1.0


def test_compare_pairs_and_skips(tmp_path):
    mk(tmp_path, "20261016T024835_01c2eac0", "Encore 今天开会")
    rows = [
        {"id": "20261016T024835_01c2eac0", "text": "oncall 今天开会", "audio_s": 60, "transcribe_s": 30,
         "model": "m", "peak_footprint_mb": 1500, "thermal_state": "fair"},
        {"id": "20261016T999999_deadbeef", "text": "x", "audio_s": 5, "transcribe_s": 1},
    ]
    gl = ci._glossary_funcs()
    out = ci.compare(rows, tmp_path, gl)
    a = next(r for r in out if r["id"].endswith("01c2eac0"))
    assert a["rtf"] == 0.5
    assert a["cer_phone_vs_mac"] > 0            # Encore vs oncall differ raw
    assert a["g_cer_phone_vs_mac"] == 0.0       # glossary Encore->oncall aligns them
    assert any("skipped" in r for r in out)
    md = ci.render(out)
    assert "| 20261016T024835_01c2eac0 |" in md and "Summary" in md


def test_load_phone_last_wins(tmp_path):
    f = tmp_path / "ios_asr.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in [{"id": "a", "text": "1"}, {"id": "a", "text": "2"}]) + "\nbad\n")
    assert [r["text"] for r in ci.load_phone(f)] == ["2"]
