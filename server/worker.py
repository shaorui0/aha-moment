"""Background transcription worker (single ASR concurrency, retries, crash recovery)."""
from __future__ import annotations

import json
import logging
import os
import itertools
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from storage import Store, write_atomic

log = logging.getLogger("aha.worker")


class AsrBackend:
    """Default ASR: lazy-imports aha_asr (so the server runs without it)."""

    def __init__(self, backend: str | None = None, language: str | None = None):
        self.backend, self.language, self._mod = backend, language, None

    def _m(self):
        if self._mod is None:
            try:
                import aha_asr  # noqa
            except ImportError:
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "asr"))
                import aha_asr  # noqa
            self._mod = aha_asr
        return self._mod

    def has_speech(self, path) -> bool:
        return self._m().has_speech(str(path))

    def transcribe(self, path, backend=None, language=None) -> dict:
        return self._m().transcribe(str(path), backend=backend or self.backend,
                                    language=language or self.language)


class DailyRecordSync:
    """Rebuild context-infrastructure contexts/daily_records/*_aha.md after transcripts land.

    Debounced (a burst of chunks -> one run) and single-flight (never two runs at once; a kick during a run triggers
    one more run after it). AHA_DAILY_RECORD=0 disables (tests)."""
    SCRIPT = Path(__file__).resolve().parents[1] / "pipeline" / "sync_daily_record.sh"

    def __init__(self, debounce_s: float = 2.0):
        self.debounce_s = debounce_s
        self._due: float | None = None
        self._cv = threading.Condition()
        self._thread: threading.Thread | None = None

    def kick(self):
        if os.environ.get("AHA_DAILY_RECORD", "1") == "0":
            return
        with self._cv:
            self._due = time.monotonic() + self.debounce_s
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="aha-daily-record", daemon=True)
                self._thread.start()
            self._cv.notify()

    def _loop(self):
        while True:
            with self._cv:
                while self._due is None:
                    self._cv.wait()
                wait = self._due - time.monotonic()
                if wait > 0:
                    self._cv.wait(wait)
                    continue
                self._due = None
            try:
                r = subprocess.run([str(self.SCRIPT)], capture_output=True, text=True, timeout=120)
                if r.returncode:
                    log.warning("daily record sync failed: %s", (r.stderr or r.stdout)[-500:])
            except Exception:
                log.warning("daily record sync failed", exc_info=True)


class Worker:
    MAX_ATTEMPTS = 3          # per run (resets on restart)
    MAX_LIFETIME_ATTEMPTS = 6  # across restarts; then failed_permanent (`aha retry <id>` resets)

    def __init__(self, store: Store, asr=None, backoff=(2.0, 8.0), inbox: Path | None = None,
                 deliver=None):
        self.store, self.asr = store, asr or AsrBackend()
        self.backoff = backoff
        self.inbox = inbox or (store.root / "inbox" / "quick.jsonl")
        # PriorityQueue of (rank, seq, id): rank 0 = priority, 1 = normal, FIFO within rank.
        self.q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self.queued: set[str] = set()
        self.queued_prio: set[str] = set()
        # deliver(item_id, text): injectable; default honours AHA_PRIORITY_DELIVER=0 to disable.
        self.deliver = deliver or self._deliver_priority
        self.cond = threading.Condition()
        self.current: str | None = None
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.daily_record = DailyRecordSync()

    # -- lifecycle -----------------------------------------------------
    def start(self):
        for i in self.store.pending_ids():  # crash recovery
            m = self.store.get(i)
            if m.get("lifetime_attempts", 0) >= self.MAX_LIFETIME_ATTEMPTS:
                self.store.update(i, transcription_status="failed_permanent")
                continue
            self.store.update(i, transcription_status="pending", attempts=0)
            self.enqueue(i, bool(m.get("priority")))
        self.thread = threading.Thread(target=self._run, name="aha-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.q.put((-1, next(self._seq), None))
        if self.thread:
            self.thread.join(timeout=5)

    def enqueue(self, item_id: str, priority: bool = False):
        """Priority items jump ahead of normal ones; re-enqueue with priority upgrades a queued item."""
        with self.cond:
            if item_id in self.queued and not (priority and item_id not in self.queued_prio):
                return
            self.queued.add(item_id)
            if priority:
                self.queued_prio.add(item_id)
            self.q.put((0 if priority else 1, next(self._seq), item_id))

    def depth(self) -> int:
        with self.cond:
            return len(self.queued)

    def wait_done(self, item_id: str, timeout: float) -> dict | None:
        end = time.time() + timeout
        with self.cond:
            while True:
                m = self.store.get(item_id)
                if m and m["transcription_status"] in ("done", "silent", "failed", "failed_permanent"):
                    return m
                left = end - time.time()
                if left <= 0:
                    return None
                self.cond.wait(min(left, 1.0))

    # -- processing ----------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            _, _, i = self.q.get()
            if i is None:
                break
            with self.cond:
                if i not in self.queued:   # stale duplicate left by a priority upgrade
                    continue
            self.current = i
            try:
                self.process(i)
            except Exception:
                log.exception("unexpected error processing %s", i)
            finally:
                with self.cond:
                    self.queued.discard(i)
                    self.queued_prio.discard(i)
                    self.current = None
                    self.cond.notify_all()

    def process(self, item_id: str):
        s = self.store
        m = s.get(item_id)
        if not m or m["transcription_status"] in ("done", "silent", "failed_permanent"):
            return
        audio = s.audio_path(m)
        while True:
            m = s.update(item_id, transcription_status="processing", attempts=m["attempts"] + 1,
                         lifetime_attempts=m.get("lifetime_attempts", 0) + 1)
            try:
                if not self.asr.has_speech(audio):
                    s.update(item_id, transcription_status="silent", error=None)
                    self._maybe_deliver(item_id, "")
                    return
                res = self.asr.transcribe(audio)
                m, _ = s.add_transcript(item_id, res)
                s.update(item_id, transcription_status="done", error=None)
                self.daily_record.kick()   # every transcript, priority or not, shows up in daily_records right away
                if m["source"] == "quick":
                    self._dispatch(item_id, res.get("text", ""))
                self._maybe_deliver(item_id, res.get("text", "") or "")
                return
            except Exception as e:  # retry with backoff
                log.warning("asr failed for %s (attempt %d): %r", item_id, m["attempts"], e)
                if m["lifetime_attempts"] >= self.MAX_LIFETIME_ATTEMPTS:
                    s.update(item_id, transcription_status="failed_permanent", error=repr(e))
                    return
                if m["attempts"] >= self.MAX_ATTEMPTS:
                    s.update(item_id, transcription_status="failed", error=repr(e))
                    return
                s.update(item_id, transcription_status="pending", error=repr(e))
                d = self.backoff[min(m["attempts"] - 1, len(self.backoff) - 1)] if self.backoff else 0
                if self._stop.wait(d):
                    return

    # -- priority delivery (never raises) --------------------------------
    def _maybe_deliver(self, item_id: str, text: str):
        try:
            m = self.store.get(item_id)
            if not m or not m.get("priority") or m.get("priority_done_at"):
                return
            self.store.update(item_id, priority_done_at=datetime.now().astimezone().isoformat())
            self.deliver(item_id, text.strip())
        except Exception:
            log.exception("priority delivery failed for %s", item_id)

    def _deliver_priority(self, item_id: str, text: str):
        if os.environ.get("AHA_PRIORITY_DELIVER", "1") == "0":
            return
        inbox = self.store.root / "inbox"
        if text:
            try:
                write_atomic(inbox / "priority" / f"{item_id}.txt", (text + "\n").encode())
                write_atomic(inbox / "LATEST.txt", (text + "\n").encode())
                with open(inbox / "priority.md", "a", encoding="utf-8") as fh:
                    fh.write(f"[{datetime.now():%H:%M:%S}] {' '.join(text.split())}\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            except Exception:
                log.exception("priority file delivery failed for %s", item_id)
        if os.environ.get("AHA_PRIORITY_NOTIFY", "1") != "0":
            body = (text[:120] + ("…" if len(text) > 120 else "")) if text else "没有识别到语音"
            try:
                subprocess.run(["osascript", "-e", "on run argv", "-e",
                                'display notification (item 1 of argv) with title "Aha 转写完成"',
                                "-e", "end run", body], timeout=10, capture_output=True)
            except Exception:
                log.warning("notification failed", exc_info=True)
        if text and os.environ.get("AHA_PRIORITY_CLIPBOARD", "0") == "1":   # opt-in
            try:
                subprocess.run(["pbcopy"], input=text.encode(), timeout=10, capture_output=True,
                               env={**os.environ, "LANG": "en_US.UTF-8"})
            except Exception:
                log.warning("clipboard failed", exc_info=True)

    def _dispatch(self, item_id: str, text: str):
        m = self.store.get(item_id)
        if m.get("dispatched"):
            return
        rec = {"id": item_id, "recorded_at": m["recorded_at"], "text": text, "path": str(self.store.dir_of(m))}
        if m.get("tags"):
            rec["tags"] = m["tags"]
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        self.inbox.parent.mkdir(parents=True, exist_ok=True)
        with open(self.inbox, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        self.store.update(item_id, dispatched=True)

    def retry(self, item_id: str) -> bool:
        """Manual retry: reset counters and re-enqueue. False if unknown id."""
        if not self.store.get(item_id):
            return False
        m = self.store.update(item_id, transcription_status="pending", attempts=0,
                              lifetime_attempts=0, error=None)
        self.enqueue(item_id, bool(m.get("priority")))
        return True

    # -- reprocess (adds a new transcript file, never overwrites) -------
    def reprocess(self, item_id: str, backend: str | None = None) -> str:
        """Returns 'written' | 'exists' | 'silent' | 'skipped'."""
        m = self.store.get(item_id)
        if not m or m["transcription_status"] == "silent":
            return "silent" if m else "skipped"
        audio = self.store.audio_path(m)
        res = self.asr.transcribe(audio, backend=backend)
        _, wrote = self.store.add_transcript(item_id, res)
        return "written" if wrote else "exists"
