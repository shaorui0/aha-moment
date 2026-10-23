"""GET /v1/jobs returns dispatch/enhance/synth/digest/claude_runs; POST /v1/tasks/done sets status=done."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
for p in (PROJECT / "server", PROJECT / "pipeline"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

import app as app_mod  # noqa: E402
import tasks_store as S  # noqa: E402


@pytest.fixture
def client(tmp_path):
    a = app_mod.create_app(data_dir=str(tmp_path), token="tk")
    with TestClient(a) as c:
        yield c, tmp_path


def _hdr():
    return {"Authorization": "Bearer tk"}


def test_jobs_endpoint_returns_shape(client):
    c, _ = client
    r = c.get("/v1/jobs", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    for key in ("server_time", "dispatch", "enhance", "synthesize_tasks", "digest", "claude_runs"):
        assert key in body, key


def test_jobs_tolerates_missing_files(client, tmp_path, monkeypatch):
    # digest / dispatch / synth logs may not exist on fresh install; still 200
    c, _ = client
    r = c.get("/v1/jobs", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["synthesize_tasks"] == {} or isinstance(body["synthesize_tasks"], dict)


def test_tasks_done_marks_status(client, tmp_path):
    c, root = client
    f = root / "inbox" / "tasks.json"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    S.save_doc(f, {"version": 2, "items": [
        {"id": "t1", "title": "x", "next": "", "area": "个人", "topic": "其他", "evidence": [],
         "status": "open", "created": now, "updated": now},
        {"id": "t2", "title": "y", "next": "", "area": "个人", "topic": "其他", "evidence": [],
         "status": "claude", "created": now, "updated": now},
    ]})
    r = c.post("/v1/tasks/done", json={"ids": ["t1", "t2"]}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"done": 2}
    doc = S.load_doc(f)
    assert {i["id"]: i["status"] for i in doc["items"]} == {"t1": "done", "t2": "done"}


def test_tasks_done_rejects_bad_body(client):
    c, _ = client
    assert c.post("/v1/tasks/done", json={"ids": "nope"}, headers=_hdr()).status_code == 400


def test_tasks_done_idempotent_on_already_done(client, tmp_path):
    c, root = client
    f = root / "inbox" / "tasks.json"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    S.save_doc(f, {"version": 2, "items": [
        {"id": "t3", "title": "z", "next": "", "area": "个人", "topic": "其他", "evidence": [],
         "status": "done", "created": now, "updated": now},
    ]})
    r = c.post("/v1/tasks/done", json={"ids": ["t3"]}, headers=_hdr())
    assert r.status_code == 200
    assert r.json() == {"done": 0}


def test_tasks_get_includes_done_flag(client, tmp_path):
    c, root = client
    f = root / "inbox" / "tasks.json"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    S.save_doc(f, {"version": 2, "items": [
        {"id": "t4", "title": "a", "next": "", "area": "个人", "topic": "其他", "evidence": [],
         "status": "done", "created": now, "updated": now},
        {"id": "t5", "title": "b", "next": "", "area": "个人", "topic": "其他", "evidence": [],
         "status": "open", "created": now, "updated": now},
    ]})
    items = {i["id"]: i for i in c.get("/v1/tasks", headers=_hdr()).json()["items"]}
    assert items["t4"]["done"] is True
    assert items["t5"]["done"] is False
