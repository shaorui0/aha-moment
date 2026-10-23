#!/usr/bin/env python3
"""Resolve wheels with pip, download each with N parallel HTTP ranges, install offline.

For slow links where one TCP stream crawls but many in parallel are fine.
Usage: fastwheel.py <venv-pip> <wheelhouse> pkg [pkg ...] [--no-deps pkg ...]
"""
import json, os, subprocess, sys, concurrent.futures as cf, urllib.request

CONN = 16


def resolve(pip, pkgs, no_deps):
    cmd = [pip, "install", "--dry-run", "--ignore-installed", "--quiet", "--report", "-"]
    if no_deps:
        cmd.append("--no-deps")
    out = subprocess.run(cmd + pkgs, check=True, capture_output=True, text=True).stdout
    return [(i["download_info"]["url"], i["download_info"]["url"].rsplit("/", 1)[1]) for i in json.loads(out)["install"]]


def size_of(url):
    req = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(req, timeout=60) as r:
        return int(r.headers["Content-Length"])


def fetch_range(url, start, end, path):
    for _ in range(30):
        have = os.path.getsize(path) if os.path.exists(path) else 0
        if have >= end - start + 1:
            return
        r = subprocess.run(["curl", "-sfL", "--max-time", "300", "-r", f"{start + have}-{end}", url],
                           capture_output=True)
        with open(path, "ab") as f:
            f.write(r.stdout)
    raise RuntimeError(f"range failed {path}")


def download(url, dest):
    if os.path.exists(dest):
        return
    n = size_of(url)
    if n < 2_000_000:
        subprocess.run(["curl", "-sfL", "--retry", "20", "-o", dest + ".part", url], check=True)
        os.rename(dest + ".part", dest)
        return
    step = -(-n // CONN)
    parts = [(i * step, min(n, (i + 1) * step) - 1, f"{dest}.p{i}") for i in range(CONN) if i * step < n]
    with cf.ThreadPoolExecutor(CONN) as ex:
        list(ex.map(lambda p: fetch_range(url, *p), parts))
    with open(dest + ".part", "wb") as out:
        for *_, p in parts:
            with open(p, "rb") as f:
                out.write(f.read())
            os.remove(p)
    assert os.path.getsize(dest + ".part") == n, "size mismatch"
    os.rename(dest + ".part", dest)


def main():
    pip, house, *rest = sys.argv[1:]
    os.makedirs(house, exist_ok=True)
    split = rest.index("--no-deps") if "--no-deps" in rest else len(rest)
    groups = [(rest[:split], False), (rest[split + 1:], True)]
    for pkgs, nd in groups:
        if not pkgs:
            continue
        wheels = resolve(pip, pkgs, nd)
        print(f"resolved {len(wheels)} files", flush=True)
        with cf.ThreadPoolExecutor(4) as ex:
            for name in ex.map(lambda w: (download(w[0], os.path.join(house, w[1])), w[1])[1], wheels):
                print("got", name, flush=True)
        cmd = [pip, "install", "--no-index", "--find-links", house] + (["--no-deps"] if nd else []) + pkgs
        subprocess.run(cmd, check=True)
    print("ALLOK")


if __name__ == "__main__":
    main()
