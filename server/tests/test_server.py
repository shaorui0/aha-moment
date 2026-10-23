import hashlib
import json
import time

from fastapi.testclient import TestClient

TOKEN = "testtoken"  # must match conftest.TOKEN

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def sha(b):
    return hashlib.sha256(b).hexdigest()


def put(c, id_, body, rec="2026-10-09T08:30:00+09:00", sha_=None, source="ambient", **h):
    hd = {"X-Aha-Sha256": sha_ or sha(body), "X-Aha-Recorded-At": rec, "X-Aha-Source": source,
          "Content-Type": "audio/m4a", **h}
    return c.put(f"/v1/audio/{id_}", content=body, headers=hd)


def wait_status(c, id_, want, t=5):
    end = time.time() + t
    while time.time() < end:
        s = c.get(f"/v1/items/{id_}").json()["meta"]["transcription_status"]
        if s in want:
            return s
        time.sleep(0.05)
    raise AssertionError(f"status stuck at {s}")


def test_health_open_and_auth(client):
    assert client.get("/v1/health", headers={"Authorization": ""}).status_code == 200
    assert TestClient(client.app).get("/v1/status").status_code == 401
    assert TestClient(client.app).put("/v1/audio/abcdefgh", content=b"x").status_code == 401
    bad = TestClient(client.app)
    assert bad.get("/v1/status", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_upload_201_200_409_422(client):
    body = b"AUDIO-ONE-xxxxxxxx"
    assert put(client, "item00001", body).status_code == 201
    r = put(client, "item00001", body)
    assert r.status_code == 200 and r.json()["status"] == "exists"
    assert put(client, "item00001", b"different").status_code == 409
    assert put(client, "item00002", body, sha_="0" * 64).status_code == 422
    assert client.get("/v1/items/item00002").status_code == 404  # nothing persisted
    assert put(client, "bad id!", body).status_code in (400, 404)
    assert put(client, "short", body).status_code == 400
    assert client.put("/v1/audio/item00003", content=body).status_code == 400  # no sha


def test_atomic_layout(client, tmp_path):
    body = b"AUDIO-LAYOUT-xx"
    assert put(client, "item00001", body, rec="2026-10-09T23:59:00+00:00").status_code == 201
    wait_status(client, "item00001", {"done"})
    m = client.get("/v1/items/item00001").json()["meta"]
    d = tmp_path / "data" / m["dir"]
    assert (d / "audio.m4a").read_bytes() == body and (d / "meta.json").exists()
    assert m["dir"].count("/") == 3 and m["dir"].endswith("/item00001")
    assert list((tmp_path / "data" / ".tmp").iterdir()) == []  # no leftover temp
    assert not list(d.glob(".tmp-*"))


def test_worker_transcript(client, tmp_path):
    put(client, "item00001", b"hello-audio-bytes")
    assert wait_status(client, "item00001", {"done"}) == "done"
    r = client.get("/v1/items/item00001").json()
    assert r["transcript"].startswith("hello hello-au")
    d = tmp_path / "data" / r["meta"]["dir"]
    assert (d / "transcript.fake-model.txt").exists()
    assert json.loads((d / "transcript.fake-model.json").read_text())["model"] == "fake-model"
    st = client.get("/v1/status").json()
    assert st["counts"]["done"] == 1 and st["recent"][0]["id"] == "item00001"


def test_silent_skips_asr(client, asr):
    put(client, "item00001", b"SILENT-audio")
    assert wait_status(client, "item00001", {"silent"}) == "silent"
    assert asr.calls == []
    assert client.get("/v1/items/item00001").json()["transcript"] is None


def test_retry_then_fail(client, asr):
    asr.fail_times = 2
    put(client, "item00001", b"retry-audio-bytes")
    assert wait_status(client, "item00001", {"done", "failed"}) == "done"
    assert client.get("/v1/items/item00001").json()["meta"]["attempts"] == 3
    asr.fail_times = 99
    put(client, "item00002", b"fail-audio-bytes!")
    assert wait_status(client, "item00002", {"done", "failed"}) == "failed"


def test_crash_recovery(mk, asr, tmp_path):
    asr.fail_times = 99  # first run: item ends failed
    with TestClient(mk()) as c:
        c.headers.update(AUTH)
        put(c, "item00001", b"recover-me-please")
        wait_status(c, "item00001", {"failed"})
    asr.fail_times = 0  # "restart": startup scan re-enqueues failed
    with TestClient(mk()) as c:
        c.headers.update(AUTH)
        assert wait_status(c, "item00001", {"done"}) == "done"
    # pending left by a crash is also picked up
    p = next((tmp_path / "data").glob("*/*/*/item00001/meta.json"))
    m = json.loads(p.read_text()); m["transcription_status"] = "processing"; p.write_text(json.dumps(m))
    n = len(asr.calls)
    with TestClient(mk()) as c:
        c.headers.update(AUTH)
        assert wait_status(c, "item00001", {"done"}) == "done"
    assert len(asr.calls) == n + 1


def test_quick_wait_and_inbox(client, tmp_path):
    r = client.post("/v1/quick?wait=1", content=b"quick-note-bytes", headers={"Content-Type": "audio/m4a"})
    assert r.status_code == 200 and r.json()["text"].startswith("hello quick-no")
    lines = (tmp_path / "data" / "inbox" / "quick.jsonl").read_text().splitlines()
    assert len(lines) == 1
    j = json.loads(lines[0])
    assert j["id"] == r.json()["id"] and j["text"] == r.json()["text"] and "recorded_at" in j and j["path"]
    m = client.get(f"/v1/items/{j['id']}").json()["meta"]
    assert m["source"] == "quick"


def test_quick_multipart_nowait(client):
    r = client.post("/v1/quick", files={"file": ("a.m4a", b"multipart-bytes-1", "audio/m4a")})
    assert r.status_code == 201
    assert wait_status(client, r.json()["id"], {"done"}) == "done"


def test_ambient_not_in_inbox(client, tmp_path):
    put(client, "item00001", b"ambient-audio-bytes")
    wait_status(client, "item00001", {"done"})
    assert not (tmp_path / "data" / "inbox" / "quick.jsonl").exists()


def test_reprocess_adds_second_file(client, asr, tmp_path):
    put(client, "item00001", b"reproc-audio-bytes")
    wait_status(client, "item00001", {"done"})
    w = client.app.state.worker
    d = tmp_path / "data" / client.get("/v1/items/item00001").json()["meta"]["dir"]
    first = (d / "transcript.fake-model.txt").read_text()
    assert w.reprocess("item00001", backend="alt") == "written"
    assert w.reprocess("item00001", backend="alt") == "exists"  # never overwrite
    assert (d / "transcript.fake-model-alt.txt").exists()
    assert (d / "transcript.fake-model.txt").read_text() == first
    assert len(client.get("/v1/items/item00001").json()["meta"]["transcripts"]) == 2


# ---- web UI / items list / audio / attempt cap ----
def test_index_page_open(client):
    r = TestClient(client.app).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"] and "MediaRecorder" in r.text


def test_items_list_by_date(client):
    put(client, "item00001", b"list-audio-bytes-1", rec="2026-10-09T08:30:00+09:00")
    put(client, "item00002", b"list-audio-bytes-2", rec="2026-10-08T08:30:00+09:00")
    wait_status(client, "item00001", {"done"})
    r = client.get("/v1/items?date=2026-10-09")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == ["item00001"]
    assert items[0]["transcript"].startswith("hello list-aud") and items[0]["status"] == "done"
    assert client.get("/v1/items?date=bogus").status_code == 400
    assert TestClient(client.app).get("/v1/items?date=2026-10-09").status_code == 401


def test_audio_auth_header_and_query(client):
    body = b"audio-bytes-for-playback"
    put(client, "item00001", body)
    anon = TestClient(client.app)
    assert anon.get("/v1/items/item00001/audio").status_code == 401
    assert anon.get("/v1/items/item00001/audio?t=wrong").status_code == 401
    r = anon.get(f"/v1/items/item00001/audio?t={TOKEN}")
    assert r.status_code == 200 and r.content == body and r.headers["content-type"] == "audio/mp4"
    assert client.get("/v1/items/item00001/audio").content == body
    assert anon.get("/v1/items/nope0000/audio", headers=AUTH).status_code == 404
    rg = anon.get(f"/v1/items/item00001/audio?t={TOKEN}", headers={"Range": "bytes=0-4"})
    assert rg.status_code == 206 and rg.content == body[:5]


def test_lifetime_cap_failed_permanent(mk, asr):
    asr.fail_times = 10**6
    for run in range(2):  # 2 runs x 3 attempts = 6 lifetime
        with TestClient(mk()) as c:
            c.headers.update(AUTH)
            if run == 0:
                put(c, "item00001", b"cap-audio-bytes-xx")
            want = {"failed"} if run == 0 else {"failed_permanent"}
            wait_status(c, "item00001", want)
    m = None
    n = len(asr.calls)
    with TestClient(mk()) as c:  # third restart: must NOT retry
        c.headers.update(AUTH)
        time.sleep(0.3)
        m = c.get("/v1/items/item00001").json()["meta"]
        assert m["transcription_status"] == "failed_permanent" and m["lifetime_attempts"] == 6
        assert len(asr.calls) == n
        # manual retry resets and succeeds
        asr.fail_times = 0
        r = c.post("/v1/items/item00001/retry")
        assert r.status_code == 200
        assert wait_status(c, "item00001", {"done"}) == "done"
        assert c.get("/v1/items/item00001").json()["meta"]["lifetime_attempts"] == 1
        assert c.post("/v1/items/nope00000/retry").status_code == 404


def test_quick_wait_failed_returns_502(client, asr):
    asr.fail_times = 100
    r = client.post("/v1/quick?wait=1", content=b"quick-fail-bytes", headers={"Content-Type": "audio/m4a"})
    assert r.status_code == 502
    j = r.json()
    assert j["status"] == "failed" and "boom" in j["error"] and j["id"]


def test_quick_wait_timeout_returns_202(client, asr, monkeypatch):
    import time as _t
    import app as appmod
    monkeypatch.setattr(appmod, "QUICK_WAIT_TIMEOUT", 0.2)
    orig = asr.transcribe
    asr.transcribe = lambda *a, **k: (_t.sleep(1.0), orig(*a, **k))[1]
    r = client.post("/v1/quick?wait=1", content=b"quick-slow-bytes", headers={"Content-Type": "audio/m4a"})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    assert wait_status(client, r.json()["id"], {"done"}, t=5) == "done"


def test_startup_cleans_stale_tmp(tmp_path, asr):
    import os
    import time as _t
    from app import create_app
    tmpd = tmp_path / "data" / ".tmp"
    tmpd.mkdir(parents=True)
    old, fresh = tmpd / "up-old", tmpd / "up-fresh"
    old.write_bytes(b"x"); fresh.write_bytes(b"y")
    t = _t.time() - 2 * 3600
    os.utime(old, (t, t))
    with TestClient(create_app(data_dir=tmp_path / "data", token=TOKEN, asr=asr, backoff=())):
        pass
    assert not old.exists() and fresh.exists()


def test_adts_aac_kept_as_aac():
    """iOS writes ADTS .aac chunks (crash-safe); stored as audio.aac, not mislabeled m4a."""
    from storage import ext_for
    assert ext_for("audio/aac") == "aac"
    assert ext_for("audio/m4a") == "m4a"


def test_tags_header_put_to_meta(client):
    assert put(client, "tagged001", b"AUDIO-TAG-xxxxxxxx", **{"X-Aha-Tags": "claude"}).status_code == 201
    assert client.get("/v1/items/tagged001").json()["meta"]["tags"] == ["claude"]
    assert put(client, "tagged002", b"AUDIO-TAG-yyyyyyyy", **{"X-Aha-Tags": " Claude, foo ,claude,"}).status_code == 201
    assert client.get("/v1/items/tagged002").json()["meta"]["tags"] == ["claude", "foo"]


def test_tags_absent_means_no_key(client):
    assert put(client, "notag0001", b"AUDIO-NOTAG-xxxxxxx").status_code == 201
    assert "tags" not in client.get("/v1/items/notag0001").json()["meta"]
    assert put(client, "notag0002", b"AUDIO-NOTAG-yyyyyyy", **{"X-Aha-Tags": " , "}).status_code == 201
    assert "tags" not in client.get("/v1/items/notag0002").json()["meta"]


def test_tags_quick_meta_and_inbox(client, tmp_path):
    r = client.post("/v1/quick?wait=1", content=b"quick-tagged-bytes",
                    headers={"Content-Type": "audio/m4a", "X-Aha-Tags": "claude"})
    assert r.status_code == 200
    j = json.loads((tmp_path / "data" / "inbox" / "quick.jsonl").read_text().splitlines()[0])
    assert j["tags"] == ["claude"]
    assert client.get(f"/v1/items/{j['id']}").json()["meta"]["tags"] == ["claude"]
    r2 = client.post("/v1/quick?wait=1", content=b"quick-plain-bytes!", headers={"Content-Type": "audio/m4a"})
    j2 = json.loads((tmp_path / "data" / "inbox" / "quick.jsonl").read_text().splitlines()[1])
    assert j2["id"] == r2.json()["id"] and "tags" not in j2


def test_session_tags_merge_idempotent_404_auth(client):
    sess = {"X-Aha-Session": "sess-0001"}
    assert put(client, "chunk0001", b"AUDIO-S-aaaaaaaaaa", **sess).status_code == 201
    assert put(client, "chunk0002", b"AUDIO-S-bbbbbbbbbb", **sess, **{"X-Aha-Tags": "foo"}).status_code == 201
    assert put(client, "other0001", b"AUDIO-S-cccccccccc", **{"X-Aha-Session": "sess-0002"}).status_code == 201
    url = "/v1/sessions/sess-0001/tags"
    r = client.post(url, json={"tags": ["Claude"]})
    assert r.status_code == 200 and r.json()["items"] == 2
    meta = lambda i: client.get(f"/v1/items/{i}").json()["meta"]
    assert meta("chunk0001")["tags"] == ["claude"]
    assert meta("chunk0002")["tags"] == ["foo", "claude"]
    assert "tags" not in meta("other0001")
    assert client.post(url, json={"tags": ["claude"]}).status_code == 200      # idempotent
    assert meta("chunk0002")["tags"] == ["foo", "claude"]
    # quick item: session id == item id
    qid = client.post("/v1/quick", content=b"quick-bytes-xxxxxx", headers={"Content-Type": "audio/m4a"}).json()["id"]
    assert client.post(f"/v1/sessions/{qid}/tags", json={"tags": ["claude"]}).status_code == 200
    assert meta(qid)["tags"] == ["claude"]
    assert client.post("/v1/sessions/nosuchsess/tags", json={"tags": ["claude"]}).status_code == 404
    assert client.post(url, json={"tags": "claude"}).status_code == 400
    assert TestClient(client.app).post(url, json={"tags": ["claude"]}).status_code == 401
