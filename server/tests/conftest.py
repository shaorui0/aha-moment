import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from app import create_app

TOKEN = "testtoken"


class FakeASR:
    def __init__(self):
        self.calls, self.speech, self.fail_times, self.model = [], True, 0, "fake-model"

    def has_speech(self, path):
        return self.speech and b"SILENT" not in open(path, "rb").read()

    def transcribe(self, path, backend=None, language=None):
        self.calls.append((str(path), backend))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("boom")
        model = f"{self.model}-{backend}" if backend else self.model
        return {"text": "hello " + open(path, "rb").read()[:8].decode(errors="ignore"),
                "language": "en", "segments": [], "model": model, "backend": backend or "fake",
                "duration_s": 1.0, "elapsed_s": 0.1}


@pytest.fixture
def asr():
    return FakeASR()


@pytest.fixture
def mk(tmp_path, asr):
    """Factory: returns TestClient context for the same data dir (to simulate restarts)."""
    def _mk(start_worker=True):
        app = create_app(data_dir=tmp_path / "data", token=TOKEN, asr=asr, backoff=())
        return app
    return _mk


@pytest.fixture
def client(mk):
    with TestClient(mk()) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setenv("AHA_PRIORITY_NOTIFY", "0")
    monkeypatch.setenv("AHA_PRIORITY_CLIPBOARD", "0")
    monkeypatch.setenv("AHA_DAILY_RECORD", "0")
