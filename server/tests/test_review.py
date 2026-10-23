import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from conftest import TOKEN
from test_server import put, wait_status, AUTH

import claude_tasks as T

ENH = "<!-- derived -->\n\n**[task]** 给 dispatch 加重试。\n\n**[smalltalk]** 天气不错。\n\n## 纠正\n\n- 低八 → dispatch\n"


@pytest.fixture(autouse=True)
def ws(tmp_path, monkeypatch):
    w = tmp_path / "ws"
    monkeypatch.setenv("AHA_CONTEXT_ROOT", str(w))
    monkeypatch.setattr(T, "trigger_enhance", lambda src, root: triggered.append(src.name))
    triggered.clear()
    return w


triggered: list = []


def item(client, iid="rev00001"):
    assert put(client, iid, b"VOICE001-body", source="quick").status_code == 201
    wait_status(client, iid, {"done"})
    return iid


def daily_src(client, ws, iid, enh=ENH):
    m = client.get(f"/v1/items/{iid}").json()["meta"]
    rec = T.build_rec(iid, [dict(m, _dir=Path(client.app.state.store.dir_of(m)))], datetime.now().astimezone())
    name = T.export_name(client.app.state.store.root, rec)
    d = ws / "contexts" / "daily_records"
    (d / "enhanced").mkdir(parents=True, exist_ok=True)
    src = d / name
    src.write_text("hello\n")
    if enh is not None:
        e = d / "enhanced" / (name[:-3] + ".enhance.md")
        e.write_text(enh)
        os.utime(src, (1000, 1000)); os.utime(e, (2000, 2000))
    return name


def test_auth_and_404(client):
    from fastapi.testclient import TestClient
    anon = TestClient(client.app)
    assert anon.get("/v1/sessions/abcdefgh/text").status_code == 401
    assert anon.post("/v1/sessions/abcdefgh/claude", json={"text": "x"}).status_code == 401
    assert client.get("/v1/sessions/nosuchsess/text").status_code == 404
    assert client.post("/v1/sessions/nosuchsess/claude", json={"text": "x"}).status_code == 404
    assert client.get("/v1/sessions/bad!/text").status_code == 400


def test_pending_then_ready(client, ws):
    iid = item(client)
    j = client.get(f"/v1/sessions/{iid}/text").json()
    assert j["status"] == "pending" and "hello" in j["text"] and "edited_text" not in j
    daily_src(client, ws, iid, enh=None)
    assert client.get(f"/v1/sessions/{iid}/text").json()["status"] == "pending"
    assert triggered                                    # enhance fired, non-blocking
    name = daily_src(client, ws, iid)
    j = client.get(f"/v1/sessions/{iid}/text").json()
    assert j["status"] == "ready" and j["text"] == "给 dispatch 加重试。"
    assert j["version"] == T.V_ENHANCED and j["source_file"] == name


def test_fallback_after_wait(client, ws, monkeypatch):
    iid = item(client)
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", -1)
    monkeypatch.setattr(T, "glossary_text", lambda t: t.upper())
    j = client.get(f"/v1/sessions/{iid}/text").json()
    assert j["status"] == "fallback" and j["text"].startswith("HELLO") and j["version"] == T.V_GLOSSARY


def test_post_claude_stores_text_tags_and_wins(client, ws, monkeypatch):
    iid = item(client)
    assert client.post(f"/v1/sessions/{iid}/claude", json={"text": "  "}).status_code == 400
    assert client.post(f"/v1/sessions/{iid}/claude", json={}).status_code == 400
    r = client.post(f"/v1/sessions/{iid}/claude", json={"text": "我改过的文字"})
    assert r.status_code == 200 and r.json()["items"] == 1 and r.json()["already_started"] is False
    assert client.get(f"/v1/items/{iid}").json()["meta"]["tags"] == ["claude"]
    assert client.get(f"/v1/sessions/{iid}/text").json()["edited_text"] == "我改过的文字"
    # claude_tasks: edited text has top priority and never waits for enhance
    root = client.app.state.store.root
    monkeypatch.setattr(T, "ENHANCE_WAIT_S", 600)
    daily_src(client, ws, iid)                          # an enhanced file exists too; still loses
    m = client.get(f"/v1/items/{iid}").json()["meta"]
    rec = T.build_rec(iid, [dict(m, _dir=Path(client.app.state.store.dir_of(m)))], datetime.now().astimezone())
    rec["ready_at"] = datetime.now().astimezone()       # fresh: would defer without edited text
    assert T.resolve_text(root, rec, datetime.now().astimezone(), log=lambda *_: None) == ("我改过的文字", T.V_USER)
