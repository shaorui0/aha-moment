#!/usr/bin/env python3
"""Pick the cp313/macOS-arm64 wheel URL for each package (latest version) from the PyPI JSON API."""
import json, sys, urllib.request
for pkg in sys.argv[1:]:
    d = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=60))
    ok = [u for u in d["urls"] if u["filename"].endswith(".whl") and ("macosx" in u["filename"] or "none-any" in u["filename"]) and ("arm64" in u["filename"] or "universal2" in u["filename"] or "none-any" in u["filename"])
          and ("cp313" in u["filename"] or "py3-none" in u["filename"] or "abi3" in u["filename"])]
    ok.sort(key=lambda u: ("macosx_15" not in u["filename"], "macosx_14" not in u["filename"]))
    print(ok[0]["url"] if ok else f"NONE {pkg}")
