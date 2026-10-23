"""Screenshot / photo recognition (#10, approach A): OCR (Apple Vision) -> qwen3:14b JSON -> ledger.

Layout: <data>/images/YYYY/MM/DD/<id>/{image.<ext>, meta.json};  results are appended to <data>/images/ledger.jsonl.
Nothing here writes anywhere else (no reminders, no daily_records): the ledger is read-only input for a later UI.
All LLM traffic goes through the shared Ollama gate (tools/ollama_gate.py).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from storage import ID_RE, write_json_atomic

sys.path.insert(0, os.environ.get("OLLAMA_GATE_DIR", str(Path(__file__).resolve().parents[3] / "tools")))
import ollama_gate  # noqa: E402

log = logging.getLogger("aha.images")

KINDS = ("receipt", "asset", "table", "photo")
EXT_BY_TYPE = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/heic": "heic",
               "image/heif": "heic", "image/webp": "webp", "image/gif": "gif", "image/tiff": "tiff"}
OCR_BIN = Path(os.environ.get("AHA_OCR_BIN") or Path(__file__).resolve().parents[1] / "tools" / "ocr" / "ocr")
TEXT_MODEL = os.environ.get("AHA_IMG_TEXT_MODEL", "qwen3:14b")
VLM_MODEL = os.environ.get("AHA_IMG_VLM_MODEL", "qwen3-vl:8b")
LANGS_JA = "ja-JP,zh-Hans,en-US"
LANGS_ZH = "zh-Hans,ja-JP,en-US"
MAX_ATTEMPTS = 5

PROMPTS = {
    "receipt": ('下面是一张收据/付款截图的 OCR 文字。提取 JSON，键：date（YYYY-MM-DD，没有则 null）、amount（数字，实付总额，'
                '不含货币符号和千分位逗号）、currency（ISO 代码，如 JPY/CNY/USD）、merchant、'
                'category（food/transport/shopping/bills/income/other 之一）。只依据 OCR 文字，不要猜。只输出 JSON。'),
    "asset": ('下面是一张资产/账户截图（银行、券商、支付 App 等）的 OCR 文字。余额就是财务信息，必须提取。'
              '提取 JSON，键：institution（机构名）、account（账户名或脱敏尾号）、balance（数字，不含货币符号和千分位逗号）、'
              'currency（ISO 代码）、as_of（YYYY-MM-DD，没有则 null）。只依据 OCR 文字，不要猜。只输出 JSON。'),
    "table": ('下面是一张表格截图的 OCR 文字。把每一行还原成一个对象，输出 JSON：{"rows": [ {列名: 值, ...}, ... ]}。'
              '列名取表头；数字保持 OCR 原样的数值，不要计算、不要补全。只输出 JSON。'),
}
KEY_FIELDS = {"receipt": ("amount", "date"), "asset": ("balance", "as_of")}
VLM_PROMPT = "用中文一两句话描述这张照片的内容（场景、主要物体、可见文字）。只输出描述。"
CJK_RE = re.compile(r"[぀-ヿ㐀-鿿]")


# ------------------------------------------------------------------ OCR

def run_ocr(path: Path, langs: str) -> list[dict]:
    r = subprocess.run([str(OCR_BIN), str(path), "--langs", langs], capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"ocr failed: {r.stderr.strip()[:300]}")
    out = []
    for ln in r.stdout.splitlines():
        try:
            j = json.loads(ln)
            out.append({"text": str(j["text"]), "confidence": float(j.get("confidence", 0))})
        except (ValueError, KeyError):
            continue
    return out


def mean_conf(lines: list[dict]) -> float:
    return sum(l["confidence"] for l in lines) / len(lines) if lines else 0.0


def cjk_lowconf(lines: list[dict], thr: float = 0.5) -> int:
    return sum(1 for l in lines if l["confidence"] <= thr and CJK_RE.search(l["text"]))


def ocr_best(path: Path, ocr=run_ocr, many: int = 3) -> tuple[list[dict], str, float]:
    """ja-first; if many CJK lines are low-confidence rerun zh-first and keep the higher mean confidence."""
    lines, langs = ocr(path, LANGS_JA), "ja"
    if cjk_lowconf(lines) >= many:
        alt = ocr(path, LANGS_ZH)
        if mean_conf(alt) > mean_conf(lines):
            lines, langs = alt, "zh"
    return lines, langs, round(mean_conf(lines), 3)


# ------------------------------------------------------------------ extraction + verification

def parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    return json.loads(t)


def _digits(s: str) -> str:
    """Digits-only view of OCR text where thousands separators / spaces are dropped, decimal point kept."""
    return re.sub(r"[,，\s]", "", s)


def number_in_ocr(value, ocr_text: str) -> bool:
    if value is None or isinstance(value, bool):
        return False
    try:
        num = float(str(value).replace(",", ""))
    except ValueError:
        return False
    cands = {f"{num:.2f}", f"{num:.1f}", str(int(num)) if num == int(num) else repr(num), f"{num:g}"}
    if num == int(num):
        cands.add(f"{int(num)}.00")
    flats = [_digits(l) for l in ocr_text.splitlines()]   # per line: never glue digits of neighbouring lines
    return any(re.search(rf"(?<![\d.]){re.escape(c)}(?![\d]|\.\d)", f) for c in cands for f in flats)


MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def date_in_ocr(value, ocr_text: str) -> bool:
    """y, m, d each appear in the OCR text: as numbers (2026/10/9, 2026年10月9日) or with an English month name."""
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", str(value or ""))
    if not m:
        return False
    y, mo, d = (int(x) for x in m.groups())
    for line in ocr_text.splitlines():
        nums = [int(x) for x in re.findall(r"\d+", line)]
        low = line.lower()
        if y in nums and d in nums and (mo in nums or re.search(MONTHS[mo - 1] + r"[a-z]*", low)):
            return True
    nums = [int(x) for x in re.findall(r"\d+", ocr_text)]   # fall back: pieces on different lines
    return y in nums and mo in nums and d in nums


def verify(kind: str, data, ocr_text: str) -> list[str]:
    """-> reasons the result needs human review (empty = looks consistent with the OCR text)."""
    why = []
    if kind in KEY_FIELDS:
        if not isinstance(data, dict):
            return ["not an object"]
        if data.get("kind") == "not_financial":
            return ["model says not financial"]
        for f in KEY_FIELDS[kind]:
            v = data.get(f)
            if v is None:
                if f in ("amount", "balance"):
                    why.append(f"{f} missing")
                continue
            ok = date_in_ocr(v, ocr_text) if f in ("date", "as_of") else number_in_ocr(v, ocr_text)
            if not ok:
                why.append(f"{f}={v!r} not verbatim in OCR")
    elif kind == "table":
        if not isinstance(data, list) or not data:
            return ["no rows"]
        for i, row in enumerate(data):
            for k, v in (row.items() if isinstance(row, dict) else []):
                num = isinstance(v, (int, float)) and not isinstance(v, bool) or (
                    isinstance(v, str) and re.fullmatch(r"[¥$€]?-?[\d,]+(\.\d+)?", v.strip()))
                if num and not number_in_ocr(str(v).strip().lstrip("¥$€"), ocr_text):
                    why.append(f"row {i} {k}={v!r} not verbatim in OCR")
    return why


def extract(kind: str, lines: list[dict], gate=ollama_gate):
    """-> (data, needs_review_reasons). Raises on gate/HTTP errors (caller retries)."""
    ocr_text = "\n".join(l["text"] for l in lines)
    if not ocr_text.strip():
        return None, ["no text found by OCR"]
    r = gate.chat(TEXT_MODEL, [{"role": "user", "content": f"{PROMPTS[kind]}\n\nOCR 文字：\n{ocr_text}"}],
                  {"temperature": 0}, fmt="json", keep_alive="2m")
    try:
        data = parse_json(gate.content(r))
    except ValueError:
        return None, ["model output is not JSON"]
    if kind == "table":
        data = data.get("rows") if isinstance(data, dict) else data
    return data, verify(kind, data, ocr_text)


def vlm_jpeg(path: Path) -> Path:
    """Downscaled JPEG copy for the VLM (HEIC/huge screenshots would blow the context)."""
    out = Path(tempfile.mkdtemp(prefix="aha-vlm-")) / "in.jpg"
    r = subprocess.run(["sips", "-Z", "1280", "-s", "format", "jpeg", str(path), "--out", str(out)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode or not out.exists():
        raise RuntimeError(f"sips failed: {r.stderr.strip()[:200]}")
    return out


def caption(path: Path, gate=ollama_gate, to_jpeg=vlm_jpeg):
    """-> text, or None if the gate deferred (memory too tight)."""
    import base64
    jp = to_jpeg(path)
    try:
        b64 = base64.b64encode(Path(jp).read_bytes()).decode()
    finally:
        Path(jp).unlink(missing_ok=True)
    r = gate.chat(VLM_MODEL, [{"role": "user", "content": VLM_PROMPT, "images": [b64]}], {"temperature": 0.2})
    if r.get("status") == "deferred":
        return None
    return gate.content(r)


# ------------------------------------------------------------------ store

class ImageStore:
    def __init__(self, data_root: Path):
        self.root = Path(data_root) / "images"
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".tmp").mkdir(exist_ok=True)
        self.ledger = self.root / "ledger.jsonl"
        self.lock = threading.RLock()
        self.items: dict[str, dict] = {}
        for mp in self.root.glob("[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*/meta.json"):
            try:
                m = json.loads(mp.read_text())
                self.items[m["id"]] = m
            except Exception:
                continue

    def tmp_path(self) -> Path:
        return self.root / ".tmp" / f"up-{os.urandom(6).hex()}"

    def dir_of(self, m: dict) -> Path:
        return self.root / m["dir"]

    def image_path(self, m: dict) -> Path:
        return self.dir_of(m) / m["file"]

    def get(self, i: str) -> dict | None:
        with self.lock:
            m = self.items.get(i)
            return json.loads(json.dumps(m)) if m else None

    def commit(self, item_id: str, tmp: Path, sha: str, *, kind: str, ext: str, size: int, recorded_at: datetime):
        with self.lock:
            cur = self.items.get(item_id)
            if cur:
                os.unlink(tmp)
                return ("conflict" if cur["sha256"] != sha else "exists"), json.loads(json.dumps(cur))
            rel = f"{recorded_at:%Y/%m/%d}/{item_id}"
            d = self.root / rel
            d.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, d / f"image.{ext}")
            m = {"id": item_id, "kind": kind, "recorded_at": recorded_at.isoformat(),
                 "received_at": datetime.now().astimezone().isoformat(), "sha256": sha, "size": size,
                 "dir": rel, "file": f"image.{ext}", "status": "pending", "attempts": 0, "error": None,
                 "needs_review": None, "result": None}
            write_json_atomic(d / "meta.json", m)
            self.items[item_id] = m
            return "created", json.loads(json.dumps(m))

    def update(self, item_id: str, **fields) -> dict:
        with self.lock:
            m = self.items[item_id]
            m.update(fields)
            write_json_atomic(self.dir_of(m) / "meta.json", m)
            return json.loads(json.dumps(m))

    def ids_with(self, *statuses) -> list[str]:
        with self.lock:
            return [i for i, m in self.items.items() if m["status"] in statuses]

    def append_ledger(self, rec: dict) -> None:
        with self.lock, open(self.ledger, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def since(self, ts: datetime | None) -> list[dict]:
        with self.lock:
            rows = [m for m in self.items.values()
                    if ts is None or datetime.fromisoformat(m["received_at"]) >= ts]
        rows.sort(key=lambda m: m["received_at"])
        keys = ("id", "kind", "status", "recorded_at", "received_at", "needs_review", "review_reasons", "result", "error")
        return [{k: m.get(k) for k in keys} for m in rows]


# ------------------------------------------------------------------ worker

class ImageWorker:
    """Single thread. Deferred/transient-failed items are retried every retry_s while the queue is idle."""

    def __init__(self, store: ImageStore, ocr=run_ocr, gate=ollama_gate, to_jpeg=vlm_jpeg, retry_s: float = 600):
        self.store, self.ocr, self.gate, self.to_jpeg, self.retry_s = store, ocr, gate, to_jpeg, retry_s
        self.q: queue.Queue = queue.Queue()
        self.queued: set[str] = set()
        self.cond = threading.Condition()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self):
        for i in self.store.ids_with("pending", "processing", "deferred"):
            self.enqueue(i)
        self.thread = threading.Thread(target=self._run, name="aha-image-worker", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.q.put(None)
        if self.thread:
            self.thread.join(timeout=5)

    def enqueue(self, i: str):
        with self.cond:
            if i in self.queued:
                return
            self.queued.add(i)
        self.q.put(i)

    def wait_idle(self, timeout: float = 10) -> bool:
        end = time.time() + timeout
        with self.cond:
            while self.queued:
                if time.time() >= end:
                    return False
                self.cond.wait(0.1)
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                i = self.q.get(timeout=self.retry_s)
            except queue.Empty:
                for d in self.store.ids_with("deferred"):
                    self.enqueue(d)
                continue
            if i is None:
                break
            try:
                self.process(i)
            except Exception:
                log.exception("unexpected error processing image %s", i)
            finally:
                with self.cond:
                    self.queued.discard(i)
                    self.cond.notify_all()

    def process(self, item_id: str):
        s = self.store
        m = s.get(item_id)
        if not m or m["status"] in ("done", "failed"):
            return
        m = s.update(item_id, status="processing", attempts=m["attempts"] + 1)
        path, kind = s.image_path(m), m["kind"]
        try:
            if kind == "photo":
                cap = caption(path, self.gate, self.to_jpeg)
                if cap is None:
                    s.update(item_id, status="deferred", error="vlm deferred (memory)")
                    return
                self._finish(m, {"caption": cap}, [], model=VLM_MODEL)
                return
            lines, langs, conf = ocr_best(path, self.ocr)
            data, why = extract(kind, lines, self.gate)
            self._finish(m, data, why, model=TEXT_MODEL, ocr={"langs": langs, "mean_conf": conf, "lines": len(lines)})
        except Exception as e:
            log.warning("image %s failed (attempt %d): %r", item_id, m["attempts"], e)
            s.update(item_id, status="failed" if m["attempts"] >= MAX_ATTEMPTS else "deferred", error=repr(e))

    def _finish(self, m: dict, data, why: list[str], **info):
        review = bool(why)
        m = self.store.update(m["id"], status="done", error=None, result=data, needs_review=review,
                              review_reasons=why)
        self.store.append_ledger({"id": m["id"], "kind": m["kind"], "recorded_at": m["recorded_at"],
                                  "processed_at": datetime.now().astimezone().isoformat(), "data": data,
                                  "needs_review": review, "review_reasons": why, **info})


# ------------------------------------------------------------------ routes

def router(store: ImageStore, worker: ImageWorker, auth, parse_ts, spool) -> APIRouter:
    r = APIRouter(prefix="/v1", dependencies=[Depends(auth)])

    @r.put("/image/{item_id}")
    async def put_image(item_id: str, request: Request, kind: str = "receipt"):
        if not ID_RE.match(item_id):
            raise HTTPException(400, "id must match ^[A-Za-z0-9_-]{8,64}$")
        if kind not in KINDS:
            raise HTTPException(400, f"kind must be one of {KINDS}")
        want = (request.headers.get("x-aha-sha256") or "").strip().lower()
        if not want:
            raise HTTPException(400, "X-Aha-Sha256 header required")
        try:
            rec = parse_ts(request.headers.get("x-aha-recorded-at"))
        except ValueError:
            raise HTTPException(400, "X-Aha-Recorded-At must be ISO8601")
        tmp, sha, size, ctype = await spool(request, store.tmp_path())
        if sha != want:
            tmp.unlink(missing_ok=True)
            raise HTTPException(422, f"sha256 mismatch (server computed {sha})")
        ext = EXT_BY_TYPE.get((ctype or "").split(";")[0].strip().lower(), "png")
        res, m = store.commit(item_id, tmp, sha, kind=kind, ext=ext, size=size, recorded_at=rec)
        if res == "conflict":
            raise HTTPException(409, "id exists with different content")
        if res == "created":
            worker.enqueue(item_id)
            return JSONResponse({"status": "created", "id": item_id, "kind": kind}, status_code=201)
        return {"status": "exists", "id": item_id}

    @r.get("/images")
    def list_images(since: str | None = None):
        try:
            ts = parse_ts(since) if since else None
        except ValueError:
            raise HTTPException(400, "since must be ISO8601")
        return {"items": store.since(ts)}

    return r
