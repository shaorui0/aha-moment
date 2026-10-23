"""Compare on-device (iPhone WhisperKit) transcripts against the Mac's for the SAME audio.

Usage: python compare_ios.py [--jsonl ~/aha_data/bench/ios_asr.jsonl] [--data ~/aha_data] [--out BENCH_IOS.md]

Phone results come from POST /v1/bench/asr (server/bench_asr.py). Mac transcript = first
<data>/YYYY/MM/DD/<id>/transcript.*.txt. CER is bench.py's definition (norm + edit distance / len(ref)), computed both
ways (ref=Mac / ref=phone; neither is ground truth, so it is a disagreement measure), raw and after pipeline/glossary.txt.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import re
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
GLOSSARY = HERE.parent / "pipeline" / "glossary.txt"


def norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", s.lower())


def cer(ref: str, hyp: str) -> float:
    """Same as bench.py (kept in sync; bench.py has a __main__ guard but we avoid importing its heavy path)."""
    r, h = list(norm(ref)), list(norm(hyp))
    if not r:
        return 0.0 if not h else float(len(h))
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def _glossary_funcs():
    """Reuse pipeline/correct_classify's glossary logic (read-only import). Returns apply(text)->text."""
    spec = importlib.util.spec_from_file_location("_cc", HERE.parent / "pipeline" / "correct_classify.py")
    import sys
    sys.path.insert(0, str(HERE.parent / "pipeline"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hard, _ = mod.parse_glossary(GLOSSARY.read_text(encoding="utf-8"))
    return lambda t: mod.apply_glossary(t, hard)[0]


def load_phone(jsonl: Path) -> list[dict]:
    rows = []
    if jsonl.exists():
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    last = {}
    for r in rows:   # a re-run of the same id replaces the earlier result
        last[r["id"]] = r
    return list(last.values())


def mac_transcript(data: Path, rid: str) -> str | None:
    m = re.match(r"(\d{4})(\d{2})(\d{2})T", rid)
    cands = glob.glob(str(data / m.group(1) / m.group(2) / m.group(3) / rid / "transcript.*.txt")) if m else []
    cands = cands or glob.glob(str(data / "*" / "*" / "*" / rid / "transcript.*.txt"))
    return Path(sorted(cands)[0]).read_text(encoding="utf-8").strip() if cands else None


def compare(phone_rows: list[dict], data: Path, gloss=None) -> list[dict]:
    out = []
    for p in sorted(phone_rows, key=lambda r: r["id"]):
        mac = mac_transcript(data, p["id"])
        if mac is None or p.get("error"):
            out.append({"id": p["id"], "skipped": "no Mac transcript" if mac is None else f"phone error: {p['error']}"})
            continue
        ph = p.get("text", "")
        row = {"id": p["id"], "audio_s": p.get("audio_s") or 0.0, "model": p.get("model", ""),
               "load_s": p.get("load_s"), "transcribe_s": p.get("transcribe_s") or 0.0,
               "peak_mb": p.get("peak_footprint_mb"), "thermal": p.get("thermal_state"),
               "battery": p.get("battery_level"), "mac": mac, "phone": ph,
               "cer_phone_vs_mac": cer(mac, ph), "cer_mac_vs_phone": cer(ph, mac)}
        row["rtf"] = row["transcribe_s"] / row["audio_s"] if row["audio_s"] else 0.0
        if gloss:
            gm, gp = gloss(mac), gloss(ph)
            row["g_cer_phone_vs_mac"], row["g_cer_mac_vs_phone"] = cer(gm, gp), cer(gp, gm)
        out.append(row)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.mean(xs) if xs else float("nan")


def render(rows: list[dict]) -> str:
    ok = [r for r in rows if "skipped" not in r]
    L = ["# iPhone on-device ASR vs Mac (same audio)", "",
         "Phone: WhisperKit on-device; Mac: mlx whisper-large-v3-turbo (production backend). CER definition = `bench.py` "
         "(strip punctuation/space/case, char edit distance / len(ref)). Neither side is ground truth, so CER is a "
         "*disagreement* measure, reported both ways; `g-` columns apply `pipeline/glossary.txt` hard replacements to both "
         "sides first. RTF = transcribe_s / audio_s (excl. model load).", "",
         "| id | model | audio s | transcribe s | RTF | CER phone→Mac | CER Mac→phone | g-CER phone→Mac | g-CER Mac→phone | peak MB | thermal |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    f = lambda v, d=2: "-" if v is None else f"{v:.{d}f}"
    for r in rows:
        if "skipped" in r:
            L.append(f"| {r['id']} | skipped: {r['skipped']} | | | | | | | | | |")
            continue
        L.append(f"| {r['id']} | {r['model'].replace('large-v3-v20240930_', '')} | {r['audio_s']:.0f} | {r['transcribe_s']:.1f} | "
                 f"{r['rtf']:.2f} | {r['cer_phone_vs_mac']:.3f} | {r['cer_mac_vs_phone']:.3f} | "
                 f"{f(r.get('g_cer_phone_vs_mac'), 3)} | {f(r.get('g_cer_mac_vs_phone'), 3)} | "
                 f"{f(r['peak_mb'], 0)} | {r['thermal'] or '-'} |")
    L += ["", "## Summary", ""]
    if ok:
        L += [f"- recordings compared: {len(ok)} (skipped {len(rows) - len(ok)})",
              f"- model(s): {', '.join(sorted({r['model'] for r in ok}))}",
              f"- mean CER phone→Mac {_mean([r['cer_phone_vs_mac'] for r in ok]):.3f} / Mac→phone "
              f"{_mean([r['cer_mac_vs_phone'] for r in ok]):.3f}; median phone→Mac "
              f"{statistics.median([r['cer_phone_vs_mac'] for r in ok]):.3f}",
              f"- after glossary: mean {_mean([r.get('g_cer_phone_vs_mac') for r in ok]):.3f} / "
              f"{_mean([r.get('g_cer_mac_vs_phone') for r in ok]):.3f}",
              f"- mean RTF {_mean([r['rtf'] for r in ok]):.2f} (total audio {sum(r['audio_s'] for r in ok):.0f}s, "
              f"transcribe {sum(r['transcribe_s'] for r in ok):.0f}s)",
              f"- model load {f(ok[0].get('load_s'), 1)} s (first record); peak footprint max "
              f"{f(max((r['peak_mb'] or 0) for r in ok), 0)} MB; thermal states: "
              f"{', '.join(sorted({r['thermal'] or '?' for r in ok}))}"]
        worst = max(ok, key=lambda r: r["cer_phone_vs_mac"])
        L += ["", "## Worst pair", "", f"`{worst['id']}` CER {worst['cer_phone_vs_mac']:.3f}", "",
              f"- Mac:   {worst['mac'][:300]}", f"- Phone: {worst['phone'][:300]}"]
    else:
        L.append("- no comparable results")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/aha_data"))
    ap.add_argument("--jsonl")
    ap.add_argument("--out", default=str(HERE / "BENCH_IOS.md"))
    a = ap.parse_args()
    data = Path(a.data)
    jsonl = Path(a.jsonl) if a.jsonl else data / "bench" / "ios_asr.jsonl"
    try:
        gloss = _glossary_funcs()
    except Exception as e:  # noqa
        print("glossary unavailable:", e)
        gloss = None
    md = render(compare(load_phone(jsonl), data, gloss))
    Path(a.out).write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
