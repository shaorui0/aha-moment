import json


def make_item(root, day, iid, hhmmss, text, source="ambient", status="done", dur=60000, tz="+00:00"):
    y, m, d = day.split("-")
    d_ = root / y / m / d / iid
    d_.mkdir(parents=True, exist_ok=True)
    meta = {"id": iid, "source": source, "recorded_at": f"{day}T{hhmmss}{tz}", "duration_ms": dur,
            "transcription_status": status, "dir": f"{y}/{m}/{d}/{iid}",
            "transcripts": [{"slug": "m1"}] if text is not None else []}
    (d_ / "meta.json").write_text(json.dumps(meta))
    if text is not None:
        (d_ / "transcript.m1.txt").write_text(text)
    return d_
