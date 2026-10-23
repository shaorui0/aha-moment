import hashlib
import time

import pytest
from fastapi.testclient import TestClient

import images
from app import create_app

TOKEN = "testtoken"

PNG = b"\x89PNG fake image bytes"


class FakeGate:
    def __init__(self, replies=None, defer=False):
        self.calls, self.replies, self.defer = [], list(replies or []), defer

    def chat(self, model, messages, options=None, **kw):
        self.calls.append((model, messages, kw))
        if images.ollama_gate.is_vlm(model):
            return {"status": "deferred", "reason": "mem"} if self.defer else {"status": "ok", "message": {"content": "a cat"}}
        return {"status": "ok", "message": {"content": self.replies.pop(0)}}

    @staticmethod
    def content(r):
        return r["message"]["content"]


def L(text, c=1.0):
    return {"text": text, "confidence": c}


class FakeOcr:
    def __init__(self, ja, zh=None):
        self.ja, self.zh, self.calls = ja, zh, []

    def __call__(self, path, langs):
        self.calls.append(langs)
        return self.zh if langs.startswith("zh") and self.zh is not None else self.ja


def fake_jpeg(p):
    q = p.with_suffix(".jpg")
    q.write_bytes(b"j")
    return q


def mk(tmp_path, ocr, gate, retry=600):
    app = create_app(data_dir=tmp_path / "data", token=TOKEN, asr=None, backoff=(), ocr=ocr, gate=gate,
                     to_jpeg=fake_jpeg,
                     img_retry_s=retry)
    return app


def put(c, i, kind, body=PNG):
    return c.put(f"/v1/image/{i}?kind={kind}", content=body,
                 headers={"X-Aha-Sha256": hashlib.sha256(body).hexdigest(), "Content-Type": "image/png"})


def run(tmp_path, kind, ocr, gate, i="img-00000001"):
    app = mk(tmp_path, ocr, gate)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        assert put(c, i, kind).status_code == 201
        assert app.state.image_worker.wait_idle(10)
        return c, c.get("/v1/images").json()["items"], app


def ledger(app):
    import json
    return [json.loads(l) for l in app.state.images.ledger.read_text().splitlines()]


def test_receipt_verified(tmp_path):
    c, items, app = run(tmp_path, "receipt", FakeOcr([L("スーパー谷駅前店"), L("2026年10月3日"), L("合計 ¥1,887")]),
                        FakeGate(['{"date":"2026-10-03","amount":1887,"currency":"JPY","merchant":"谷駅前店","category":"shopping"}']))
    assert items[0]["status"] == "done" and items[0]["needs_review"] is False
    assert ledger(app)[0]["data"]["amount"] == 1887


def test_receipt_amount_not_in_ocr_needs_review(tmp_path):
    _, items, app = run(tmp_path, "receipt", FakeOcr([L("合計 ¥887"), L("2026/10/03")]),
                        FakeGate(['{"date":"2026-10-03","amount":1887,"currency":"JPY","merchant":"x","category":"food"}']))
    assert items[0]["needs_review"] is True
    assert any("amount" in r for r in items[0]["review_reasons"])


def test_asset_balance_and_sha_idempotent(tmp_path):
    gate = FakeGate(['{"institution":"X銀行","account":"****4567","balance":1284560,"currency":"JPY","as_of":"2026-10-15"}'])
    c, items, app = run(tmp_path, "asset", FakeOcr([L("残高"), L("¥1,284,560"), L("2026/10/15 現在")]), gate)
    assert items[0]["needs_review"] is False
    assert "財務" not in gate.calls[0][1][0]["content"] and "余额" in gate.calls[0][1][0]["content"]
    assert put(c, "img-00000001", "asset").json()["status"] == "exists"
    r = c.put("/v1/image/img-00000001?kind=asset", content=b"other",
              headers={"X-Aha-Sha256": hashlib.sha256(b"other").hexdigest()})
    assert r.status_code == 409
    assert len(ledger(app)) == 1


def test_table_rows(tmp_path):
    _, items, _ = run(tmp_path, "table", FakeOcr([L("品名 数量"), L("A 3"), L("B 12")]),
                      FakeGate(['{"rows":[{"品名":"A","数量":3},{"品名":"B","数量":99}]}']))
    assert len(items[0]["result"]) == 2 and items[0]["needs_review"] is True


def test_zh_rerun_when_cjk_lowconf(tmp_path):
    ja = [L("星巴克", .3), L("咖啡", .3), L("支付成功", .4), L("¥38.00")]
    zh = [L("星巴克", .9), L("咖啡", .9), L("支付成功", 1), L("¥38.00")]
    ocr = FakeOcr(ja, zh)
    _, items, app = run(tmp_path, "receipt", ocr, FakeGate(['{"date":null,"amount":38.00,"currency":"CNY","merchant":"星巴克","category":"food"}']))
    assert ocr.calls == [images.LANGS_JA, images.LANGS_ZH]
    assert ledger(app)[0]["ocr"]["langs"] == "zh" and items[0]["needs_review"] is False


def test_no_rerun_when_confident(tmp_path):
    ocr = FakeOcr([L("星巴克"), L("¥38.00")])
    run(tmp_path, "receipt", ocr, FakeGate(['{"amount":38}']))
    assert ocr.calls == [images.LANGS_JA]


def test_photo_deferred_then_retried(tmp_path):
    gate = FakeGate(defer=True)
    app = mk(tmp_path, FakeOcr([]), gate, retry=0.2)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        put(c, "img-00000002", "photo")
        app.state.image_worker.wait_idle(5)
        assert c.get("/v1/images").json()["items"][0]["status"] == "deferred"
        gate.defer = False
        for _ in range(50):
            if c.get("/v1/images").json()["items"][0]["status"] == "done":
                break
            time.sleep(0.1)
        it = c.get("/v1/images").json()["items"][0]
        assert it["status"] == "done" and it["result"] == {"caption": "a cat"}


def test_non_json_and_empty_ocr_need_review(tmp_path):
    _, items, _ = run(tmp_path, "receipt", FakeOcr([L("hello")]), FakeGate(["not json"]))
    assert items[0]["needs_review"] is True
    _, items, _ = run(tmp_path / "b", "receipt", FakeOcr([]), FakeGate())
    assert items[0]["needs_review"] is True


def test_auth_kind_and_since(tmp_path):
    app = mk(tmp_path, FakeOcr([L("1")]), FakeGate(['{"amount":1}']))
    with TestClient(app) as c:
        assert c.get("/v1/images").status_code == 401
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        assert put(c, "img-00000003", "bogus").status_code == 400
        assert c.put("/v1/image/img-00000003?kind=receipt", content=PNG).status_code == 400   # no sha
        put(c, "img-00000003", "receipt")
        assert c.get("/v1/images?since=2999-01-01").json()["items"] == []
        assert len(c.get("/v1/images?since=2020-01-01").json()["items"]) == 1


@pytest.mark.parametrize("v,text,ok", [(1887, "¥1,887", True), (38.0, "¥-38.00", True), (887, "¥1,887", False),
                                        (118.94, "Total 118.94 USD", True), (12800, "12,800円", True)])
def test_number_in_ocr(v, text, ok):
    assert images.number_in_ocr(v, text) is ok


def test_date_with_month_name_and_string_table_cells():
    assert images.date_in_ocr("2026-10-09", "Date: October 9, 2026")
    assert not images.date_in_ocr("2026-10-19", "Date: October 9, 2026")
    assert images.verify("table", [{"Amount": "12,800"}], "12,800円") == []
    assert images.verify("table", [{"Amount": "12,900"}], "12,800円")
