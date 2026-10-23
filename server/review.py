"""Phone-side review: GET /v1/sessions/{sid}/text (Mac-corrected text) and POST /v1/sessions/{sid}/claude
(user-edited text -> sidecar + tag). Text selection reuses pipeline/claude_tasks.py (resolve_text helpers)."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import claude_tasks as T  # noqa: E402
import storage  # noqa: E402

MAX_TEXT = 200_000


def router(store, auth) -> APIRouter:
    r = APIRouter()

    def metas_of(sid: str) -> list[dict]:
        if not storage.ID_RE.match(sid):
            raise HTTPException(400, "bad session id")
        with store.lock:
            ms = [dict(m, _dir=store.dir_of(m)) for m in store.items.values() if (m.get("session_id") or m["id"]) == sid]
        if not ms:
            raise HTTPException(404, "unknown session")
        ms.sort(key=lambda m: m.get("recorded_at") or "")
        return ms

    @r.get("/v1/sessions/{sid}/text", dependencies=[Depends(auth)])
    def session_text(sid: str):
        ms = metas_of(sid)
        mine = T.read_user_text(store.root, sid)
        base = {"status": "pending", "version": None, "text": "", "source_file": None}
        if mine is not None:
            base["edited_text"] = mine
        if any(m.get("transcription_status") not in T.TERMINAL for m in ms):
            return {**base, "version": "转写中"}
        now = datetime.now().astimezone()
        rec = T.build_rec(sid, ms, now)
        if rec is None:
            return {**base, "status": "ready", "version": "无语音内容"}
        body, name = T.enhanced_text(store.root, rec)      # fire-and-forget enhance if missing/stale
        if body is not None:
            return {**base, "status": "ready", "version": T.V_ENHANCED, "text": body, "source_file": name}
        g = T.glossary_text(rec["text"])
        fb, ver = (g, T.V_GLOSSARY) if g is not None else (rec["text"], T.V_RAW)
        waited = (now - rec["ready_at"]).total_seconds()
        # pending: `text` is the uncorrected preview; fallback: correction round timed out, same text claude_tasks would send
        return {**base, "status": "fallback" if waited >= T.ENHANCE_WAIT_S else "pending",
                "version": ver, "text": fb, "source_file": name}

    @r.post("/v1/sessions/{sid}/claude", dependencies=[Depends(auth)])
    async def send_to_claude(sid: str, request: Request):
        metas_of(sid)
        try:
            body = await request.json()
            text = body.get("text") if isinstance(body, dict) else None
        except ValueError:
            text = None
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, 'body must be {"text": "<non-empty>"}')
        if len(text) > MAX_TEXT:
            raise HTTPException(413, "text too long")
        T.write_user_text(store.root, sid, text)
        ids = store.add_session_tags(sid, ["claude"])
        started = any(x.get("recording_id") == sid for x in T.read_runs(store.root))
        return {"session_id": sid, "tags": ["claude"], "items": len(ids), "already_started": started}

    return r
