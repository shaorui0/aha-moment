"""Benchmark every working backend over samples/*.{m4a,wav}.

Usage: python bench.py [--backends a,b] [--out BENCH.md]
Each backend runs in its own subprocess so peak RSS / first-load time are isolated.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def norm(s: str) -> str:
    s = s.lower()
    return re.sub(r"[\s\W_]+", "", s)


def cer(ref: str, hyp: str) -> float:
    r, h = list(norm(ref)), list(norm(hyp))
    if not r:
        return 0.0 if not h else float(len(h))  # silence: any output counts as insertions
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


def worker(backend: str, files: list[str]):
    import resource
    import aha_asr
    rows = []
    t0 = time.time()
    first = True
    load_s = None
    for f in files:
        try:
            res = aha_asr.transcribe(f, backend=backend)
            if first:
                # first call includes model load (+ possible download if uncached)
                load_s = time.time() - t0 - res["elapsed_s"]
                first = False
            rows.append({"file": os.path.basename(f), "text": res["text"], "dur": res["duration_s"],
                         "elapsed": res["elapsed_s"], "model": res["model"]})
        except Exception as e:  # noqa
            rows.append({"file": os.path.basename(f), "error": repr(e)})
            first = False
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6  # bytes on macOS -> MB
    print("RESULT" + json.dumps({"rows": rows, "rss_mb": rss, "load_s": load_s}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends")
    ap.add_argument("--out", default=os.path.join(HERE, "BENCH.md"))
    ap.add_argument("--worker")
    a = ap.parse_args()
    files = sorted(f for ext in ("m4a", "wav") for f in glob.glob(os.path.join(HERE, "samples", f"*.{ext}")))
    if a.worker:
        return worker(a.worker, files)
    import aha_asr
    backends = a.backends.split(",") if a.backends else aha_asr.list_backends()
    lines = ["| backend | file | CER | RTF | peak RSS MB | first-load s | output |",
             "|---|---|---|---|---|---|---|"]
    for b in backends:
        p = subprocess.run([sys.executable, __file__, "--worker", b], capture_output=True, text=True)
        m = re.search(r"^RESULT(.*)$", p.stdout, re.M)
        if not m:
            lines.append(f"| {b} | - | FAILED | - | - | - | {p.stderr.strip().splitlines()[-1:] } |")
            continue
        d = json.loads(m.group(1))
        for r in d["rows"]:
            if "error" in r:
                lines.append(f"| {b} | {r['file']} | ERR | - | - | - | {r['error'][:80]} |")
                continue
            refp = os.path.join(HERE, "samples", os.path.splitext(r["file"])[0] + ".ref.txt")
            c = f"{cer(open(refp).read().strip(), r['text']):.2f}" if os.path.exists(refp) else "n/a"
            rtf = r["elapsed"] / r["dur"] if r["dur"] else 0
            lines.append(f"| {b} | {r['file']} | {c} | {rtf:.3f} | {d['rss_mb']:.0f} | "
                         f"{(d['load_s'] or 0):.1f} | {r['text'][:60].replace('|', '/')} |")
    out = "\n".join(lines)
    print(out)
    with open(a.out, "w") as f:
        f.write("# ASR bench (synthetic `say` samples)\n\nCER = char edit distance after stripping punctuation/space/case; "
                "silence ref is empty so any output = hallucination. RTF = elapsed/audio duration (incl. VAD+ffmpeg). "
                "RSS = whole-process peak for that backend. first-load = model load time (cache warm unless noted).\n\n"
                + out + "\n")


if __name__ == "__main__":
    main()
