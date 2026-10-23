"""Parallel ranged HF downloader into the default HF cache layout. usage: hfpar.py repo [pattern...]"""
import json, os, sys, urllib.request, threading, time, fnmatch
repo = sys.argv[1]; pats = sys.argv[2:] or ["*"]
HF = os.path.expanduser("~/.cache/huggingface/hub/models--" + repo.replace("/", "--"))
def get(url, rng=None, tries=200):
    for t in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Range": rng} if rng else {})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:
            time.sleep(1)
    raise RuntimeError("fail " + url)
info = json.loads(get(f"https://huggingface.co/api/models/{repo}?blobs=true"))
sha = info["sha"]
os.makedirs(f"{HF}/blobs", exist_ok=True); os.makedirs(f"{HF}/refs", exist_ok=True)
open(f"{HF}/refs/main", "w").write(sha)
CH = 512 * 1024
for s in info["siblings"]:
    fn = s["rfilename"]
    if not any(fnmatch.fnmatch(fn, p) for p in pats): continue
    size = s["size"]; etag = (s.get("lfs") or {}).get("sha256") or s["blobId"]
    blob = f"{HF}/blobs/{etag}"; snap = f"{HF}/snapshots/{sha}/{fn}"
    os.makedirs(os.path.dirname(snap), exist_ok=True)
    if not os.path.exists(blob):
        tmp = blob + ".part"
        with open(tmp, "wb") as f: f.truncate(size)
        chunks = [(o, min(o + CH, size) - 1) for o in range(0, size, CH)]
        lock = threading.Lock(); done = [0]; it = iter(chunks)
        fd = os.open(tmp, os.O_WRONLY)
        def work():
            while True:
                with lock:
                    c = next(it, None)
                if c is None: return
                data = get(f"https://huggingface.co/{repo}/resolve/main/{fn}", f"bytes={c[0]}-{c[1]}")
                os.pwrite(fd, data, c[0])
                with lock:
                    done[0] += len(data)
        ths = [threading.Thread(target=work) for _ in range(48)]
        [t.start() for t in ths]
        while any(t.is_alive() for t in ths):
            time.sleep(10); print(f"{repo}/{fn} {done[0]/size:.1%}", flush=True)
        os.close(fd); os.rename(tmp, blob)
    if not os.path.lexists(snap): os.symlink(os.path.relpath(blob, os.path.dirname(snap)), snap)
print("DONE", repo, flush=True)
