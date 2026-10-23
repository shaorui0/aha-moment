import json

from fastapi.testclient import TestClient


def test_prompt_matches_mac_backend(client):
    r = client.get("/v1/asr/prompt")
    assert r.status_code == 200
    assert "简体中文" in r.json()["prompt"]


def test_bench_post_appends_jsonl(client, tmp_path):
    for i in (1, 2):
        r = client.post("/v1/bench/asr", json={"id": f"rec{i}", "text": "你好", "model": "m", "transcribe_s": 1.5})
        assert r.status_code == 200 and r.json()["id"] == f"rec{i}"
    lines = (tmp_path / "data" / "bench" / "ios_asr.jsonl").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(x) for x in lines]
    assert [x["id"] for x in rows] == ["rec1", "rec2"]
    assert rows[0]["transcribe_s"] == 1.5 and "received_at" in rows[0]


def test_bench_validation_and_auth(client):
    assert client.post("/v1/bench/asr", json={"text": "x"}).status_code == 400
    assert client.post("/v1/bench/asr", json={"id": "a", "text": 3}).status_code == 400
    anon = TestClient(client.app)
    assert anon.post("/v1/bench/asr", json={"id": "a", "text": "x"}).status_code == 401
    assert anon.get("/v1/asr/prompt").status_code == 401
