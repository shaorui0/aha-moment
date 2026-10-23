import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fastapi.testclient import TestClient

from app import create_app
from conftest import TOKEN, FakeASR
from test_server import put, wait_status, AUTH


class GatedASR(FakeASR):
    """Blocks inside transcribe until released; records processing order."""
    def __init__(self):
        super().__init__()
        self.gate, self.entered, self.order = threading.Event(), threading.Event(), []

    def transcribe(self, path, backend=None, language=None):
        self.order.append(open(path, "rb").read()[:8].decode())
        self.entered.set()
        self.gate.wait(10)
        return super().transcribe(path, backend, language)


def meta(c, i):
    return c.get(f"/v1/items/{i}").json()["meta"]


def test_header_persist_and_upgrade(tmp_path):
    asr = GatedASR()
    app = create_app(data_dir=tmp_path / "d", token=TOKEN, asr=asr, backoff=())
    with TestClient(app, headers=AUTH) as c:
        put(c, "blocker01", b"BLOCKER1-x")
        assert asr.entered.wait(5)
        assert put(c, "normal001", b"NORMAL01-x").status_code == 201
        assert "priority" not in meta(c, "normal001")
        assert put(c, "high00001", b"HIGH0001-x", **{"X-Aha-Priority": "high"}).status_code == 201
        assert meta(c, "high00001")["priority"] is True
        assert put(c, "normal002", b"NORMAL02-x").status_code == 201
        # re-PUT with priority upgrades the pending item (idempotent 200)
        r = put(c, "normal002", b"NORMAL02-x", **{"X-Aha-Priority": "high"})
        assert r.status_code == 200 and meta(c, "normal002")["priority"] is True
        asr.gate.set()
        wait_status(c, "normal001", {"done"})
        # blocker was mid-way; then priority items (high00001 first-queued, normal002 upgraded), then normal
        assert asr.order == ["BLOCKER1", "HIGH0001", "NORMAL02", "NORMAL01"]
        # idempotent re-PUT keeps priority
        put(c, "high00001", b"HIGH0001-x")
        assert meta(c, "high00001")["priority"] is True


def test_quick_source_is_priority(client):
    assert put(client, "quick0001", b"QUICKAUD-x", source="quick").status_code == 201
    assert meta(client, "quick0001")["priority"] is True
    wait_status(client, "quick0001", {"done"})


def test_delivery_files_and_latest(client, tmp_path):
    assert client.get("/v1/latest").status_code == 404
    put(client, "high00001", b"HIGHAAAA-x", **{"X-Aha-Priority": "high"})
    wait_status(client, "high00001", {"done"})
    deadline = time.time() + 5
    inbox = tmp_path / "data" / "inbox"
    while time.time() < deadline and not (inbox / "LATEST.txt").exists():
        time.sleep(0.05)
    assert (inbox / "priority" / "high00001.txt").read_text().strip() == "hello HIGHAAAA"
    assert (inbox / "LATEST.txt").read_text().strip() == "hello HIGHAAAA"
    assert "] hello HIGHAAAA" in (inbox / "priority.md").read_text()
    put(client, "high00002", b"HIGHBBBB-x", **{"X-Aha-Priority": "high"})
    wait_status(client, "high00002", {"done"})
    time.sleep(0.3)
    assert (inbox / "LATEST.txt").read_text().strip() == "hello HIGHBBBB"
    assert len((inbox / "priority.md").read_text().splitlines()) == 2
    r = client.get("/v1/latest").json()
    assert r["id"] == "high00002" and r["text"] == "hello HIGHBBBB" and r["done_at"] and r["recorded_at"]
    assert TestClient(client.app).get("/v1/latest").status_code == 401


def test_normal_item_no_delivery(client, tmp_path):
    put(client, "normal001", b"NORMALAA-x")
    wait_status(client, "normal001", {"done"})
    time.sleep(0.2)
    assert not (tmp_path / "data" / "inbox" / "LATEST.txt").exists()


def test_delivery_failure_does_not_fail_item(tmp_path, asr):
    def boom(i, t):
        raise RuntimeError("x")
    app = create_app(data_dir=tmp_path / "d", token=TOKEN, asr=asr, backoff=(), deliver=boom)
    with TestClient(app, headers=AUTH) as c:
        put(c, "high00001", b"HIGHAAAA-x", **{"X-Aha-Priority": "high"})
        assert wait_status(c, "high00001", {"done", "failed"}) == "done"


def test_silent_priority_notifies_only(tmp_path, asr):
    calls = []
    asr.speech = False
    app = create_app(data_dir=tmp_path / "d", token=TOKEN, asr=asr, backoff=(),
                     deliver=lambda i, t: calls.append((i, t)))
    with TestClient(app, headers=AUTH) as c:
        put(c, "high00001", b"HIGHAAAA-x", **{"X-Aha-Priority": "high"})
        wait_status(c, "high00001", {"silent"})
        time.sleep(0.3)
    assert calls == [("high00001", "")]


def test_session_headers_stored(client, tmp_path):
    import hashlib
    body = b"session-chunk-1"
    h = {"Authorization": "Bearer testtoken", "Content-Type": "audio/aac",
         "X-Aha-Sha256": hashlib.sha256(body).hexdigest(), "X-Aha-Recorded-At": "2026-10-12T18:00:00+09:00",
         "X-Aha-Session": "20261012T180000_abcd1234", "X-Aha-Session-Start": "2026-10-12T18:00:00+09:00"}
    r = client.put("/v1/audio/20261012T180000_chunk001", content=body, headers=h)
    assert r.status_code == 201
    import json
    m = json.loads(next((tmp_path / "data").rglob("20261012T180000_chunk001/meta.json")).read_text())
    assert m["session_id"] == "20261012T180000_abcd1234" and m["session_start"].startswith("2026-10-12T18:00")


def test_daily_record_sync_debounced(tmp_path, monkeypatch):
    """Every transcript kicks the daily-record rebuild; a burst collapses into one run."""
    import time
    import worker as W
    log = tmp_path / "runs"
    script = tmp_path / "sync.sh"
    script.write_text(f"#!/bin/sh\necho run >> {log}\n"); script.chmod(0o755)
    monkeypatch.setattr(W.DailyRecordSync, "SCRIPT", script)
    monkeypatch.setenv("AHA_DAILY_RECORD", "1")
    s = W.DailyRecordSync(debounce_s=0.2)
    for _ in range(5):
        s.kick()
    time.sleep(1.0)
    assert log.read_text().count("run") == 1
    s.kick(); time.sleep(0.8)
    assert log.read_text().count("run") == 2
