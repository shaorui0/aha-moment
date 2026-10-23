"""uvicorn app factory for the e2e harness (does NOT modify server/).

AHA_E2E_ASR=fake : deterministic stub ASR (ffmpeg volumedetect as VAD, sleep AHA_E2E_FAKE_DELAY s)
AHA_E2E_ASR=real : server's default AsrBackend (aha_asr)
AHA_DATA_DIR / AHA_TOKEN come from the environment (set by phone_sim.py to temp values).
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

import app as aha_app  # noqa: E402


class FakeAsr:
    def __init__(self, delay: float):
        self.delay = delay

    def has_speech(self, path) -> bool:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", "volumedetect",
                            "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", r.stderr)
        return bool(m) and m.group(1) != "-inf" and float(m.group(1)) > -60

    def transcribe(self, path, backend=None, language=None) -> dict:
        time.sleep(self.delay)
        return {"backend": "fake", "model": "fake-asr", "text": f"fake transcript of {Path(path).parent.name}"}


def make():
    if os.environ.get("AHA_E2E_ASR", "fake") == "fake":
        return aha_app.create_app(asr=FakeAsr(float(os.environ.get("AHA_E2E_FAKE_DELAY", "0.05"))))
    return aha_app.create_app()
