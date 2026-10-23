"""FastAPI ingest server. Use create_app() (tests) or `aha serve`."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import bench_asr
import claude_panel
import digest
import images
import jobs
import review
import storage
import tasks
import todos
from storage import ID_RE, SOURCES, Store
from web import INDEX_HTML
from worker import Worker

QUICK_WAIT_TIMEOUT = 60.0
MEDIA_BY_EXT = {"m4a": "audio/mp4", "aac": "audio/aac", "mp3": "audio/mpeg", "wav": "audio/wav", "caf": "audio/x-caf",
                "ogg": "audio/ogg", "webm": "audio/webm", "flac": "audio/flac"}


def create_app(data_dir=None, token: str | None = None, asr=None, backoff=(2.0, 8.0), deliver=None,
               ocr=None, gate=None, to_jpeg=None, img_retry_s: float = 600) -> FastAPI:
    store = Store(data_dir or storage.data_root())
    tok = token or storage.load_token()
    worker = Worker(store, asr=asr, backoff=backoff, deliver=deliver)
    img_store = images.ImageStore(store.root)
    img_worker = images.ImageWorker(img_store, ocr=ocr or images.run_ocr, gate=gate or images.ollama_gate,
                                    to_jpeg=to_jpeg or images.vlm_jpeg, retry_s=img_retry_s)

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        img_worker.start()
        yield
        img_worker.stop()
        worker.stop()

    app = FastAPI(title="Aha Moment ingest", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker
    app.state.images, app.state.image_worker = img_store, img_worker

    def auth(request: Request):
        h = request.headers.get("authorization", "")
        got = h[7:] if h.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(got.encode(), tok.encode()):
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})

    def auth_or_query(request: Request):
        """Bearer header, or ?t=<token> (for <audio> tags which cannot set headers)."""
        h = request.headers.get("authorization", "")
        got = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("t", "")
        if not hmac.compare_digest(got.encode(), tok.encode()):
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})

    async def spool(request: Request, tmp=None) -> tuple:
        """Stream the request body (raw or first multipart file) to a temp file. -> (tmp, sha, size, ctype)"""
        tmp = tmp or store.tmp_path()
        h, size = hashlib.sha256(), 0
        ctype = request.headers.get("content-type", "")
        try:
            if ctype.lower().startswith("multipart/form-data"):
                form = await request.form()
                up = next((v for v in form.values() if hasattr(v, "read")), None)
                if up is None:
                    raise HTTPException(400, "multipart body has no file part")
                ctype = up.content_type or ""
                with open(tmp, "wb") as fh:
                    while chunk := await up.read(1 << 20):
                        fh.write(chunk); h.update(chunk); size += len(chunk)
            else:
                with open(tmp, "wb") as fh:
                    async for chunk in request.stream():
                        fh.write(chunk); h.update(chunk); size += len(chunk)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        if size == 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "empty body")
        return tmp, h.hexdigest(), size, ctype

    def parse_common(request: Request, default_source: str):
        try:
            rec = storage.parse_ts(request.headers.get("x-aha-recorded-at"))
        except ValueError:
            raise HTTPException(400, "X-Aha-Recorded-At must be ISO8601")
        source = request.headers.get("x-aha-source", default_source)
        if source not in SOURCES:
            raise HTTPException(400, f"X-Aha-Source must be one of {SOURCES}")
        dur = request.headers.get("x-aha-duration-ms")
        try:
            dur = int(dur) if dur else None
        except ValueError:
            raise HTTPException(400, "X-Aha-Duration-Ms must be integer")
        return rec, source, dur

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})

    @app.get("/v1/health")
    def health():
        return {"ok": True, "queue_depth": worker.depth()}

    @app.put("/v1/audio/{item_id}", dependencies=[Depends(auth)])
    async def put_audio(item_id: str, request: Request):
        if not ID_RE.match(item_id):
            raise HTTPException(400, "id must match ^[A-Za-z0-9_-]{8,64}$")
        want = (request.headers.get("x-aha-sha256") or "").strip().lower()
        if not want:
            raise HTTPException(400, "X-Aha-Sha256 header required")
        rec, source, dur = parse_common(request, "ambient")
        tmp, sha, size, ctype = await spool(request)
        if sha != want:
            tmp.unlink(missing_ok=True)
            raise HTTPException(422, f"sha256 mismatch (server computed {sha})")
        prio = (request.headers.get("x-aha-priority", "").strip().lower() == "high") or source == "quick"
        sid = (request.headers.get("x-aha-session") or "").strip()
        session = ({"session_id": sid, "session_start": (request.headers.get("x-aha-session-start") or "").strip() or None}
                   if ID_RE.match(sid) else None)
        res, m = store.commit_upload(item_id, tmp, sha, recorded_at=rec, source=source,
                                     ext=storage.ext_for(ctype), duration_ms=dur, size=size, priority=prio,
                                     session=session, tags=storage.parse_tags(request.headers.get("x-aha-tags")))
        if res == "conflict":
            raise HTTPException(409, "id exists with different content")
        if res == "created":
            worker.enqueue(item_id, prio)
            return JSONResponse({"status": "created", "id": item_id}, status_code=201)
        if prio and m["transcription_status"] == "pending":
            worker.enqueue(item_id, True)  # upgrade a queued item
        return {"status": "exists", "id": item_id}

    @app.post("/v1/quick", dependencies=[Depends(auth)])
    async def quick(request: Request, wait: int = 0):
        rec, _, dur = parse_common(request, "quick")
        tmp, sha, size, ctype = await spool(request)
        item_id = "q" + rec.strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4)
        res, m = store.commit_upload(item_id, tmp, sha, recorded_at=rec, source="quick",
                                     ext=storage.ext_for(ctype), duration_ms=dur, size=size, priority=True,
                                     tags=storage.parse_tags(request.headers.get("x-aha-tags")))
        worker.enqueue(item_id, True)
        if not wait:
            return JSONResponse({"status": "created", "id": item_id}, status_code=201)
        done = await asyncio.to_thread(worker.wait_done, item_id, QUICK_WAIT_TIMEOUT)
        if done is None:
            return JSONResponse({"status": "pending", "id": item_id, "text": None}, status_code=202)
        if done["transcription_status"] in ("failed", "failed_permanent"):
            return JSONResponse({"status": "failed", "id": item_id, "text": None,
                                 "error": done.get("error") or "transcription failed"}, status_code=502)
        return {"status": done["transcription_status"], "id": item_id,
                "text": store.transcript_text(done) or ""}

    @app.get("/v1/status", dependencies=[Depends(auth)])
    def status():
        return {"counts": store.counts(), "queue_depth": worker.depth(),
                "total": len(store.items), "recent": store.recent(20)}

    @app.get("/v1/latest", dependencies=[Depends(auth)])
    def latest():
        m = store.latest_priority()
        if not m:
            raise HTTPException(404, "no priority item yet")
        return {"id": m["id"], "text": store.transcript_text(m) or "",
                "recorded_at": m["recorded_at"], "done_at": m["priority_done_at"]}

    @app.get("/v1/items/{item_id}", dependencies=[Depends(auth)])
    def get_item(item_id: str):
        m = store.get(item_id)
        if not m:
            raise HTTPException(404, "not found")
        return {"meta": m, "transcript": store.transcript_text(m)}

    @app.get("/v1/items", dependencies=[Depends(auth)])
    def list_items(date: str | None = None):
        day = date or datetime.now().strftime("%Y-%m-%d")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise HTTPException(400, "date must be YYYY-MM-DD")
        out = []
        for m in store.on_date(day):
            out.append({"id": m["id"], "source": m["source"], "recorded_at": m["recorded_at"],
                        "status": m["transcription_status"], "duration_ms": m.get("duration_ms"),
                        "error": m.get("error"), "transcript": store.transcript_text(m)})
        return {"date": day, "items": out}

    @app.get("/v1/items/{item_id}/audio", dependencies=[Depends(auth_or_query)])
    def get_audio(item_id: str):
        m = store.get(item_id)
        if not m:
            raise HTTPException(404, "not found")
        p = store.audio_path(m)
        if not p.exists():
            raise HTTPException(404, "audio missing")
        return FileResponse(p, media_type=MEDIA_BY_EXT.get(p.suffix.lstrip("."), "application/octet-stream"))

    @app.post("/v1/sessions/{session_id}/tags", dependencies=[Depends(auth)])
    async def tag_session(session_id: str, request: Request):
        if not ID_RE.match(session_id):
            raise HTTPException(400, "bad session id")
        try:
            body = await request.json()
            raw = body.get("tags") if isinstance(body, dict) else None
        except ValueError:
            raw = None
        if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw):
            raise HTTPException(400, 'body must be {"tags": ["claude"]}')
        tags = storage.parse_tags(",".join(raw))
        if not tags:
            raise HTTPException(400, "no valid tags")
        ids = store.add_session_tags(session_id, tags)
        if not ids:
            raise HTTPException(404, "unknown session")
        return {"session_id": session_id, "tags": tags, "items": len(ids)}

    @app.post("/v1/items/{item_id}/retry", dependencies=[Depends(auth)])
    def retry_item(item_id: str):
        if not worker.retry(item_id):
            raise HTTPException(404, "not found")
        return {"status": "requeued", "id": item_id}

    app.include_router(images.router(img_store, img_worker, auth, storage.parse_ts, spool))
    app.include_router(review.router(store, auth))
    app.include_router(claude_panel.router(store, auth))
    app.include_router(digest.router(auth))
    app.include_router(tasks.router(store.root, auth))
    app.include_router(todos.router(store.root, auth))
    import os as _os, sys as _sys
    _pipeline = str(Path(__file__).resolve().parents[1] / "pipeline")
    if _pipeline not in _sys.path:
        _sys.path.insert(0, _pipeline)
    import claude_tasks as _ct  # noqa: E402  -- for workspace path
    _records = Path(_ct.workspace()) / "contexts" / "daily_records"
    app.include_router(jobs.router(store.root, _records, auth))
    app.include_router(bench_asr.router(store.root, auth))
    return app


def app_from_env() -> FastAPI:  # for `uvicorn app:app_from_env --factory`
    return create_app()
