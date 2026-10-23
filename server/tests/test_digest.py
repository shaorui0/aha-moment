import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def out(monkeypatch, tmp_path):
    for k in ("aha", "sessions"):
        (tmp_path / k).mkdir()
    for d in range(1, 17):
        (tmp_path / "aha" / f"2026-10-{d:02d}.md").write_text(f"# aha {d}\n- x")
    (tmp_path / "aha" / "2026-10-16.sent").write_text("")
    (tmp_path / "aha" / "notes.md").write_text("ignored")
    (tmp_path / "sessions" / "2026-10-15.md").write_text("# s")
    monkeypatch.setenv("AHA_DIGEST_DIR", str(tmp_path))


def test_auth(client):
    assert TestClient(client.app).get("/v1/digest").status_code == 401


def test_latest_default_and_dates(client):
    d = client.get("/v1/digest").json()
    assert d["kind"] == "aha" and d["date"] == "2026-10-16" and d["markdown"].startswith("# aha 16")
    assert len(d["available_dates"]) == 14 and d["available_dates"][0] == "2026-10-16" and "generated_at" in d


def test_by_kind_and_date(client):
    assert client.get("/v1/digest?kind=sessions").json()["date"] == "2026-10-15"
    assert client.get("/v1/digest?date=2026-10-03").json()["markdown"].startswith("# aha 3")


def test_404_and_400(client):
    assert client.get("/v1/digest?date=2026-09-01").status_code == 404
    assert client.get("/v1/digest?kind=nope").status_code == 400
    assert client.get("/v1/digest?date=../../etc").status_code == 400


def test_empty_404(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AHA_DIGEST_DIR", str(tmp_path / "missing"))
    assert client.get("/v1/digest").status_code == 404
