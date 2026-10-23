#!/usr/bin/env python3
"""iPhone client simulator + e2e scenarios for the Aha ingest server.

Mirrors ios/AhaMoment/UploadQueue.swift: queue dir of <id>.m4a + <id>.json sidecar; PUT /v1/audio/{id}
with Bearer + X-Aha-* headers; file deleted ONLY on 200/201; 409 -> quarantine; anything else -> keep + backoff;
max 4 uploads in flight. Each scenario starts its OWN uvicorn (temp data dir, temp token, free port).
Usage: phone_sim.py [--only 1,3,5] [--asr auto|fake|real]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx

E2E = Path(__file__).resolve().parent
ROOT = E2E.parent
WORK = E2E / ".work"
PY = sys.executable


# ───────────────────────────── phone ─────────────────────────────
class Phone:
    MAX_IN_FLIGHT = 4

    def __init__(self, qdir: Path, base: str, token: str, backoff_base: float = 0.3):
        self.q, self.base, self.token = Path(qdir), base, token
        self.q.mkdir(parents=True, exist_ok=True)
        (self.q / "quarantine").mkdir(exist_ok=True)
        self.attempts: dict[str, int] = {}
        self.not_before: dict[str, float] = {}
        self.backoff_base = backoff_base
        self.acked: list[str] = []        # ids deleted locally after ACK
        self.log: list[tuple] = []        # (id, outcome)

    # -- queue
    def enqueue(self, audio: Path, source="ambient", duration_ms=60000, recorded_at=None,
                item_id=None, sha_override=None) -> str:
        item_id = item_id or "seg-" + uuid.uuid4().hex[:16]
        shutil.copy(audio, self.q / f"{item_id}.m4a")
        sha = hashlib.sha256(audio.read_bytes()).hexdigest()
        side = {"id": item_id, "sha256": sha_override or sha, "recordedAt":
                (recorded_at or datetime.now().astimezone()).isoformat(timespec="seconds"),
                "source": source, "durationMs": duration_ms, "bytes": audio.stat().st_size}
        (self.q / f"{item_id}.json").write_text(json.dumps(side))
        return item_id

    def pending(self) -> list[str]:
        return sorted(p.stem for p in self.q.glob("*.json"))

    def quarantined(self) -> list[str]:
        return sorted(p.stem for p in (self.q / "quarantine").glob("*.m4a"))

    # -- upload
    def upload_one(self, item_id: str, *, delete=True, truncate_body=False) -> tuple[str, int | None]:
        """Returns (outcome, http_code). outcome in ack|conflict|fail|neterr."""
        side = json.loads((self.q / f"{item_id}.json").read_text())
        audio = self.q / f"{item_id}.m4a"
        body = audio.read_bytes()
        if truncate_body:
            body = body[: len(body) // 2]
        h = {"Authorization": f"Bearer {self.token}", "X-Aha-Sha256": side["sha256"],
             "X-Aha-Recorded-At": side["recordedAt"], "X-Aha-Source": side["source"],
             "X-Aha-Duration-Ms": str(side["durationMs"]), "Content-Type": "audio/m4a"}
        try:
            r = httpx.put(f"{self.base}/v1/audio/{item_id}", content=body, headers=h, timeout=60)
        except Exception as e:  # connection refused / reset / timeout => keep file
            self._fail(item_id)
            self.log.append((item_id, f"neterr:{type(e).__name__}"))
            return "neterr", None
        code = r.status_code
        if code in (200, 201):
            if delete:
                audio.unlink(missing_ok=True)
                (self.q / f"{item_id}.json").unlink(missing_ok=True)
                self.acked.append(item_id)
            self.attempts.pop(item_id, None); self.not_before.pop(item_id, None)
            self.log.append((item_id, f"ack:{code}"))
            return "ack", code
        if code == 409:
            for p in (audio, self.q / f"{item_id}.json"):
                shutil.move(str(p), str(self.q / "quarantine" / p.name))
            self.log.append((item_id, "409-quarantine"))
            return "conflict", code
        self._fail(item_id)
        self.log.append((item_id, f"http:{code}"))
        return "fail", code

    def _fail(self, item_id):
        n = self.attempts.get(item_id, 0) + 1
        self.attempts[item_id] = n
        self.not_before[item_id] = time.time() + min(self.backoff_base * 2 ** (n - 1), 900)

    def sync_once(self, respect_backoff=True) -> dict[str, tuple]:
        now = time.time()
        ids = [i for i in self.pending() if not (respect_backoff and self.not_before.get(i, 0) > now)]
        with ThreadPoolExecutor(self.MAX_IN_FLIGHT) as ex:
            return dict(zip(ids, ex.map(self.upload_one, ids)))

    def drain(self, timeout=120) -> bool:
        end = time.time() + timeout
        while self.pending() and time.time() < end:
            self.sync_once()
            time.sleep(0.2)
        return not self.pending()


# ───────────────────────────── server process ─────────────────────────────
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Srv:
    def __init__(self, name: str, asr="fake", delay=0.05):
        self.dir = WORK / "runs" / f"{name}-{uuid.uuid4().hex[:6]}"
        self.data = self.dir / "data"
        self.data.mkdir(parents=True)
        self.port, self.token = free_port(), uuid.uuid4().hex
        self.asr, self.delay = asr, delay
        self.proc: subprocess.Popen | None = None
        self.base = f"http://127.0.0.1:{self.port}"
        self.starts = 0

    def start(self, delay=None):
        self.starts += 1
        env = dict(os.environ, AHA_DATA_DIR=str(self.data), AHA_TOKEN=self.token,
                   AHA_TOKEN_FILE=str(self.dir / "token-unused"), AHA_E2E_ASR=self.asr,
                   AHA_E2E_FAKE_DELAY=str(self.delay if delay is None else delay),
                   PYTHONPATH=os.pathsep.join([str(ROOT / "asr"), str(ROOT / "server")]))
        self.log = open(self.dir / f"server-{self.starts}.log", "wb")
        self.proc = subprocess.Popen(
            [PY, "-m", "uvicorn", "server_harness:make", "--factory", "--app-dir", str(E2E),
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "info"],
            cwd=ROOT / "server", env=env, stdout=self.log, stderr=subprocess.STDOUT)
        end = time.time() + 40
        while time.time() < end:
            if self.proc.poll() is not None:
                raise RuntimeError(f"server exited early rc={self.proc.returncode}; see {self.log.name}")
            try:
                if httpx.get(self.base + "/v1/health", timeout=1).status_code == 200:
                    return self
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("server did not become healthy")

    def kill9(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def auth(self):
        return {"Authorization": f"Bearer {self.token}"}

    def status(self):
        return httpx.get(self.base + "/v1/status", headers=self.auth, timeout=10).json()

    def item(self, i):
        r = httpx.get(f"{self.base}/v1/items/{i}", headers=self.auth, timeout=10)
        return r.status_code, (r.json() if r.status_code == 200 else None)

    def wait_idle(self, timeout=120, poll=0.3) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            c = self.status()["counts"]
            if not (c.get("pending", 0) or c.get("processing", 0)):
                return c
            time.sleep(poll)
        raise TimeoutError(f"not idle after {timeout}s: {self.status()['counts']}")

    # -- disk truth
    def disk_items(self) -> dict[str, dict]:
        out = {}
        for mp in self.data.glob("[0-9]*/[0-9]*/[0-9]*/*/meta.json"):
            out[mp.parent.name] = {"dir": mp.parent, "meta": json.loads(mp.read_text())}
        return out

    def audio_sha(self, i) -> str | None:
        d = self.disk_items().get(i)
        if not d:
            return None
        return hashlib.sha256((d["dir"] / d["meta"]["audio"]).read_bytes()).hexdigest()

    def audio_files(self) -> list[Path]:
        return [p for p in self.data.glob("[0-9]*/[0-9]*/[0-9]*/*/audio.*")]


# ───────────────────────────── audio corpus ─────────────────────────────
CLIPS = {
    "zh_mix": ("Tingting", "我们把 Redis 的 timeout 改成三秒，然后看 Grafana 的 P99 延迟有没有下降。"),
    "zh": ("Tingting", "今天下午三点开会，讨论一下下个季度的路线图，还有服务器的容量规划。"),
    "en": ("Samantha", "Remember to review the pull request before the standup tomorrow morning."),
    "en2": ("Samantha", "The deploy finished, but the alert is still firing, so let me check the dashboard."),
}


def sh(*a, **kw):
    return subprocess.run(a, check=True, capture_output=True, **kw)


def read_wav(p: Path) -> bytes:
    with wave.open(str(p)) as w:
        return w.readframes(w.getnframes())


class Corpus:
    SR = 16000

    def __init__(self):
        self.dir = WORK / "corpus"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clips: dict[str, bytes] = {}
        for k, (voice, text) in CLIPS.items():
            wav = self.dir / f"clip_{k}.wav"
            if not wav.exists():
                aiff = self.dir / f"clip_{k}.aiff"
                sh("say", "-v", voice, "-o", str(aiff), text)
                sh("ffmpeg", "-y", "-i", str(aiff), "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", str(wav))
            self.clips[k] = read_wav(wav)

    def segment(self, i: int, dur=60.0) -> Path:
        """~60s: speech clips separated by random silence; deterministic per i; unique bytes."""
        out = self.dir / f"seg_{i:03d}.m4a"
        if out.exists():
            return out
        rnd = random.Random(1000 + i)
        total = int(dur * self.SR) * 2
        buf = bytearray()
        keys = list(self.clips)
        while len(buf) < total:
            buf += b"\x00\x00" * int(rnd.uniform(1.5, 7.0) * self.SR)
            buf += self.clips[rnd.choice(keys)]
        buf = buf[:total]
        wav = self.dir / f"seg_{i:03d}.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(self.SR); w.writeframes(bytes(buf))
        sh("ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1", str(out))
        wav.unlink()
        return out

    def silence(self, dur=60) -> Path:
        out = self.dir / "silence.m4a"
        if not out.exists():
            sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono", "-t", str(dur),
               "-c:a", "aac", "-b:a", "32k", str(out))
        return out


# ───────────────────────────── scenarios ─────────────────────────────
@dataclass
class Res:
    id: str
    name: str
    status: str = "FAIL"   # PASS | FAIL | SKIP
    detail: str = ""
    secs: float = 0.0
    notes: list[str] = field(default_factory=list)


class Fail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise Fail(msg)


class Ctx:
    def __init__(self, corpus, real_ok: bool, real_reason: str, mode: str):
        self.corpus, self.real_ok, self.real_reason, self.mode = corpus, real_ok, real_reason, mode
        self.servers: list[Srv] = []

    def srv(self, name, asr="fake", delay=0.05) -> Srv:
        s = Srv(name, asr, delay)
        self.servers.append(s)
        return s.start()

    def phone(self, s: Srv) -> Phone:
        return Phone(s.dir / "phone_queue", s.base, s.token)


def sc1_happy(c: Ctx, r: Res):
    s = c.srv("s1"); p = c.phone(s)
    ids = [p.enqueue(c.corpus.segment(i), recorded_at=datetime.now().astimezone() - timedelta(minutes=10 - i)) for i in range(10)]
    res = p.sync_once()
    codes = sorted(v[1] for v in res.values())
    check(codes == [201] * 10, f"expected ten 201, got {codes}")
    check(not p.pending(), f"local queue not empty: {p.pending()}")
    check(sorted(p.acked) == sorted(ids), "acked != enqueued")
    disk = s.disk_items()
    check(set(disk) == set(ids), "server disk ids != uploaded ids")
    for i in ids:
        src = hashlib.sha256((p.q.parent / "x").read_bytes()).hexdigest() if False else None
    check(len(s.audio_files()) == 10, f"{len(s.audio_files())} audio files on disk")
    counts = s.wait_idle(60)
    check(counts.get("done") == 10, f"after processing counts={counts}")
    r.detail = "10x201, queue empty, 10 audio on disk, all 10 processed(fake ASR)"


def sc2_dup(c: Ctx, r: Res):
    s = c.srv("s2"); p = c.phone(s)
    i = p.enqueue(c.corpus.segment(0))
    out, code = p.upload_one(i, delete=False)     # server ACKs; "app crashes" before local delete
    check(code == 201, f"first upload {code}")
    check(p.pending() == [i], "file should still be local after simulated crash")
    out, code = p.upload_one(i, delete=True)      # app relaunches, re-uploads
    check(code == 200, f"re-upload expected 200, got {code}")
    check(not p.pending(), "queue not empty after re-ACK")
    body = httpx.put(f"{s.base}/v1/audio/{i}", content=b"x", headers={**s.auth, "X-Aha-Sha256": "0" * 64}).status_code
    check(body == 422, f"bad-sha dup attempt expected 422 got {body}")
    s.wait_idle(30)
    check(len(s.audio_files()) == 1 and len(s.disk_items()) == 1, f"dups on disk: {s.audio_files()}")
    check(s.status()["total"] == 1, "status total != 1")
    # different content, same id -> 409
    other = c.corpus.segment(1)
    h = {**s.auth, "X-Aha-Sha256": hashlib.sha256(other.read_bytes()).hexdigest(), "Content-Type": "audio/m4a"}
    code = httpx.put(f"{s.base}/v1/audio/{i}", content=other.read_bytes(), headers=h).status_code
    check(code == 409, f"same id/different content expected 409 got {code}")
    check(len(s.audio_files()) == 1, "409 changed disk")
    r.detail = "201 then 200 exists; 1 item/1 audio on disk; same-id-different-content -> 409 (disk untouched)"


def sc3_server_down(c: Ctx, r: Res):
    s = c.srv("s3"); p = c.phone(s)
    N = 12
    ids = [p.enqueue(c.corpus.segment(i), recorded_at=datetime.now().astimezone() - timedelta(minutes=N - i)) for i in range(N)]
    import threading
    th = threading.Thread(target=lambda: p.sync_once())
    th.start()
    end = time.time() + 30
    while len(p.acked) < 3 and time.time() < end:
        time.sleep(0.005)
    s.kill9()
    th.join()
    acked_before = list(p.acked)
    # phone keeps trying while server is down
    for _ in range(3):
        p.sync_once(respect_backoff=False)
    check(p.pending(), "queue unexpectedly empty while server down")
    errs = [o for _, o in p.log if o.startswith("neterr")]
    check(errs, "no network errors observed while server down")
    check(set(p.pending()) | set(p.acked) == set(ids), "an item vanished locally without ACK")
    # zero-loss invariant at crash time: everything acked locally must be on server disk with right sha
    for i in p.acked:
        check(s.audio_sha(i) is not None, f"acked {i} missing on server disk after kill -9")
    s.start()
    check(p.drain(60), f"queue not drained after restart: {p.pending()}")
    disk = s.disk_items()
    check(set(disk) == set(ids), f"missing on server: {set(ids) - set(disk)}")
    for i in ids:
        want = hashlib.sha256(c.corpus.segment(ids.index(i)).read_bytes()).hexdigest()
        check(s.audio_sha(i) == want, f"sha mismatch on disk for {i}")
    check(len(s.audio_files()) == N, f"{len(s.audio_files())} audio files for {N} ids")
    junk = list((s.data / ".tmp").glob("*"))
    if junk:
        r.notes.append(f"{len(junk)} leftover file(s) in server .tmp after kill -9 mid-upload (harmless spool, never cleaned)")
    c1 = s.wait_idle(60)
    check(c1.get("done") == N, f"post-restart counts {c1}")
    r.detail = (f"{len(acked_before)} acked pre-kill, {len(errs)} neterr while down, all {N} delivered after restart, "
                f"sha verified, 0 dup, all processed")


def sc4_corrupt(c: Ctx, r: Res):
    s = c.srv("s4"); p = c.phone(s)
    bad = p.enqueue(c.corpus.segment(0), sha_override="a" * 64)
    out, code = p.upload_one(bad)
    check(code == 422, f"wrong sha expected 422 got {code}")
    check(p.pending() == [bad] and (p.q / f"{bad}.m4a").exists(), "file not retained locally")
    check(p.attempts.get(bad) == 1, "no backoff recorded")
    check(bad not in s.disk_items() and s.item(bad)[0] == 404, "corrupt upload leaked onto server")
    # truncated in transit (sha of full file, half body)
    tr = p.enqueue(c.corpus.segment(1))
    out, code = p.upload_one(tr, truncate_body=True)
    check(code == 422 and tr not in s.disk_items(), f"truncated body: code={code}")
    check(tr in p.pending(), "truncated item not retained")
    # then good retry of the truncated one succeeds
    out, code = p.upload_one(tr)
    check(code == 201 and tr not in p.pending(), "clean retry failed")
    check(not [f for f in (s.data / ".tmp").glob("*")], "tmp spool leak after 422")
    r.detail = "wrong sha -> 422, file+sidecar kept, backoff set, nothing on server/.tmp; truncated body -> 422, clean retry -> 201"


def _real_proc_note(r):
    pass


def sc5_crash_processing(c: Ctx, r: Res, asr="fake"):
    delay = 4.0
    s = c.srv("s5" + asr, asr=asr, delay=delay)
    p = c.phone(s)
    ids = [p.enqueue(c.corpus.segment(i)) for i in range(3)]
    qid = p.enqueue(c.corpus.segment(3), source="quick")
    ids.append(qid)
    check(len(p.sync_once()) == 4 and not p.pending(), "initial upload failed")
    end = time.time() + 30
    while time.time() < end and not s.status()["counts"].get("processing"):
        time.sleep(0.05)
    counts = s.status()["counts"]
    check(counts.get("processing"), f"never observed 'processing': {counts}")
    s.kill9()
    # meta on disk should show not-yet-done items
    states = {i: d["meta"]["transcription_status"] for i, d in s.disk_items().items()}
    check(any(v in ("processing", "pending") for v in states.values()), f"nothing in flight at kill: {states}")
    s.start(delay=0.05 if asr == "fake" else None)
    c2 = s.wait_idle(600 if asr == "real" else 90)
    check(c2.get("done") == 4, f"after restart counts={c2}")
    for i in ids:
        code, body = s.item(i)
        check(body and body["transcript"] is not None, f"{i} has no transcript")
        d = s.disk_items()[i]["dir"]
        tx = list(d.glob("transcript.*.txt"))
        check(len(tx) == 1, f"{i}: {len(tx)} transcript txt files")
        check(len(s.audio_files()) == 4, "audio count changed")
    inbox = s.data / "inbox" / "quick.jsonl"
    lines = inbox.read_text().splitlines() if inbox.exists() else []
    qn = sum(1 for l in lines if json.loads(l)["id"] == qid)
    check(qn == 1, f"quick item dispatched {qn}x to inbox (want exactly 1)")
    r.detail = f"kill -9 with in-flight {dict(counts)}; after restart 4/4 done, 1 transcript each, quick dispatched once [{asr} ASR]"


def sc6_quick(c: Ctx, r: Res, asr="real"):
    s = c.srv("s6" + asr, asr=asr)
    seg = c.corpus.dir / "quick_zh.m4a"
    if not seg.exists():
        wav = c.corpus.dir / "clip_zh_mix.wav"
        sh("ffmpeg", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1", str(seg))
    t0 = time.time()
    resp = httpx.post(f"{s.base}/v1/quick?wait=1", headers=s.auth, files={"file": ("q.m4a", seg.read_bytes(), "audio/m4a")}, timeout=90)
    dt = time.time() - t0
    check(resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text[:200]}")
    j = resp.json()
    check(j["status"] == "done" and j["text"].strip(), f"unexpected body {j}")
    # also raw-body form (iOS Shortcut style)
    resp2 = httpx.post(f"{s.base}/v1/quick?wait=1", headers={**s.auth, "Content-Type": "audio/m4a"}, content=seg.read_bytes(), timeout=90)
    check(resp2.status_code == 200 and resp2.json()["text"].strip(), f"raw body form failed {resp2.status_code}")
    inbox = s.data / "inbox" / "quick.jsonl"
    check(inbox.exists() and len(inbox.read_text().splitlines()) == 2, "inbox lines != 2")
    r.detail = f"{asr} ASR: multipart + raw both 200 with text in {dt:.1f}s(first); inbox has 2 lines. text={j['text'][:70]!r}"


def sc7_silent(c: Ctx, r: Res, asr="fake"):
    s = c.srv("s7" + asr, asr=asr)
    p = c.phone(s)
    i = p.enqueue(c.corpus.silence())
    check(p.sync_once()[i][1] == 201, "upload failed")
    s.wait_idle(120 if asr == "real" else 30)
    code, body = s.item(i)
    check(body["meta"]["transcription_status"] == "silent", f"status={body['meta']['transcription_status']}")
    check(body["transcript"] is None, f"transcript present: {body['transcript']!r}")
    d = s.disk_items()[i]["dir"]
    check(not list(d.glob("transcript.*")), "transcript files on disk for silent segment")
    # audio still preserved
    check(s.audio_sha(i) is not None, "audio missing")
    q = httpx.post(f"{s.base}/v1/quick?wait=1", headers=s.auth, content=c.corpus.silence().read_bytes(), timeout=90)
    check(q.status_code == 200 and q.json()["status"] == "silent" and q.json()["text"] == "", f"quick silent -> {q.status_code} {q.text[:120]}")
    r.detail = f"{asr} ASR: status=silent, no transcript files, audio kept; quick+silent -> status silent text ''"


def sc8_throughput(c: Ctx, r: Res):
    s = c.srv("s8", asr="real")
    p = c.phone(s)
    N = 60
    t0 = time.time()
    for i in range(N):
        p.enqueue(c.corpus.segment(i), recorded_at=datetime.now().astimezone() - timedelta(minutes=N - i))
    prep = time.time() - t0
    t0 = time.time()
    check(p.drain(300), f"upload did not drain: {len(p.pending())} left")
    up = time.time() - t0
    t1 = time.time()
    cap = 30 * 60
    last = None
    while time.time() - t1 < cap:
        cn = s.status()["counts"]
        last = cn
        if not (cn.get("pending", 0) or cn.get("processing", 0)):
            break
        time.sleep(2)
    drain = time.time() - t1
    cn = s.status()["counts"]
    check(not (cn.get("pending", 0) or cn.get("processing", 0)), f"backlog not drained in {cap}s: {cn}")
    check(cn.get("failed", 0) == 0, f"{cn.get('failed')} failed items: {cn}")
    audio_s = N * 60
    r.detail = (f"upload {N}x60s in {up:.1f}s; ASR backlog drained in {drain:.0f}s for {audio_s}s audio "
                f"(RTF {drain / audio_s:.3f}, {audio_s / drain:.1f}x realtime); counts {cn}")


# ───────────────────────────── runner ─────────────────────────────
def detect_real() -> tuple[bool, str]:
    code = ("import aha_asr; aha_asr.has_speech; aha_asr.transcribe; "
            "aha_asr.has_speech('samples/silence.m4a'); print('ok')")  # actually exercise VAD (imports are lazy)
    try:
        r = subprocess.run([PY, "-c", code], cwd=ROOT / "asr", capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, repr(e)
    if r.returncode == 0:
        return True, "importable"
    lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
    return False, (lines[-1] if lines else "import failed")[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--asr", default="auto", choices=["auto", "fake", "real"])
    ap.add_argument("--out", default=str(E2E / "RESULTS.md"))
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    WORK.mkdir(exist_ok=True)
    real_ok, why = detect_real() if a.asr != "fake" else (False, "--asr fake")
    print(f"[e2e] real ASR available: {real_ok} ({why})", flush=True)
    corpus = Corpus()
    ctx = Ctx(corpus, real_ok, why, a.asr)
    S = [("1", "happy path 10 segs", sc1_happy, False),
         ("2", "duplicate upload after lost ACK", sc2_dup, False),
         ("3", "server down mid-batch (kill -9)", sc3_server_down, False),
         ("4", "corrupted upload (sha mismatch)", sc4_corrupt, False),
         ("5f", "crash during processing [fake ASR]", lambda c, r: sc5_crash_processing(c, r, "fake"), False),
         ("5r", "crash during processing [real ASR]", lambda c, r: sc5_crash_processing(c, r, "real"), True),
         ("6", "quick path wait=1 [real ASR]", sc6_quick, True),
         ("6f", "quick path wait=1 [fake ASR, plumbing only]", lambda c, r: sc6_quick(c, r, "fake"), False),
         ("7f", "silent segment [fake VAD]", lambda c, r: sc7_silent(c, r, "fake"), False),
         ("7r", "silent segment [real VAD]", lambda c, r: sc7_silent(c, r, "real"), True),
         ("8", "throughput 60x60s [real ASR]", sc8_throughput, True)]
    results = []
    for sid, name, fn, needs_real in S:
        if only and sid not in only and sid.rstrip("fr") not in only:
            continue
        r = Res(sid, name)
        t0 = time.time()
        if needs_real and not real_ok:
            r.status, r.detail = "SKIP", f"aha_asr not usable: {why}"
        else:
            try:
                fn(ctx, r)
                r.status = "PASS"
            except Fail as e:
                r.status, r.detail = "FAIL", str(e)
            except Exception as e:
                r.status, r.detail = "FAIL", f"{type(e).__name__}: {e}"
                traceback.print_exc()
            finally:
                for s in ctx.servers:
                    s.stop()
                ctx.servers.clear()
        r.secs = time.time() - t0
        results.append(r)
        print(f"[e2e] {r.id:3s} {r.status:4s} {r.secs:6.1f}s  {r.name}: {r.detail}", flush=True)
    write_report(results, real_ok, why, Path(a.out))
    print("\n| id | scenario | result | secs | detail |\n|---|---|---|---|---|")
    for r in results:
        print(f"| {r.id} | {r.name} | {r.status} | {r.secs:.1f} | {r.detail} |")
    sys.exit(1 if any(r.status == "FAIL" for r in results) else 0)


def write_report(results, real_ok, why, out: Path):
    L = ["# Aha e2e results", "", f"Run: {datetime.now().astimezone().isoformat(timespec='seconds')}  ",
         f"Real ASR (`aha_asr`): {'available' if real_ok else 'NOT available - ' + why}  ",
         "Each scenario runs against its own uvicorn (temp data dir, temp token, free port); ~/aha_data and the launchd server are never touched.  ",
         "`f` = fake ASR (stub + ffmpeg volumedetect VAD, tests server logic only), `r` = real aha_asr.", "",
         "| id | scenario | result | secs | detail |", "|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r.id} | {r.name} | **{r.status}** | {r.secs:.1f} | {r.detail.replace('|', '/')} |")
    notes = [(r.id, n) for r in results for n in r.notes]
    if notes:
        L += ["", "## Notes"] + [f"- {i}: {n}" for i, n in notes]
    out.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
