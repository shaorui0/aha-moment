import os
import sys
import time
from pathlib import Path

import pytest

import correct_classify as CC

DIGEST_DIR = Path(__file__).resolve().parents[4] / "periodic_jobs" / "nightly_digest"


def echo(prompt):
    """Stub model: FORMAT call -> echo the input block; LABEL call -> every paragraph is task."""
    if "--- 输入开始 ---" in prompt:
        return prompt.split("--- 输入开始 ---\n", 1)[1].split("\n--- 输入结束 ---", 1)[0]
    body = prompt.split("--- 段落开始 ---\n", 1)[1].split("\n--- 段落结束 ---", 1)[0]
    return "\n".join(f"{i + 1}: task" for i in range(len(body.splitlines())))


# ---------------------------------------------------------------- glossary
def test_parse_glossary():
    hard, hints = CC.parse_glossary("# c\n\nEncore→oncall\n?Cloud→Claude（指 Claude 时）\n!Spike→Spec\nbad line\n")
    assert ("Encore", "oncall") in hard and ("!Spike", "Spec") in hard
    assert hints == [("Cloud", "Claude（指 Claude 时）")]


def test_apply_glossary_hard_rules():
    hard, _ = CC.parse_glossary("Encore→oncall\nCloud Code→Claude Code\n!Spike→Spec\n二次教验→二次校验\n")
    out, hits = CC.apply_glossary("encore 和 cloud code，基于我的Spike，latency spike 不动，二次教验", hard)
    assert out == "oncall 和 Claude Code，基于我的Spec，latency spike 不动，二次校验"
    assert ("encore", "oncall") in hits


def test_guarded_cloud_rule():
    hard, _ = CC.parse_glossary("!Cloud→Claude\n")
    out, _ = CC.apply_glossary("Cloud 的信息，AWS cloud 不动，cloud native 不动，cloud provider 不动", hard)
    assert out == "Claude 的信息，AWS cloud 不动，cloud native 不动，cloud provider 不动"


def test_ascii_word_boundary():
    hard, _ = CC.parse_glossary("Encore→oncall\n")
    assert CC.apply_glossary("encores", hard)[0] == "encores"


def test_shipped_glossary_loads():
    hard, hints = CC.load_glossary()
    assert ("CircleLit", "SQLite") in hard and hints


# ---------------------------------------------------------------- passes
def test_normalize_ws_one_blank_line():
    assert CC.normalize_ws("甲\n乙\n\n\n\n丙\n") == ["甲乙", "丙"]


def test_parse_labels_defaults_missing_to_thinking():
    assert CC.parse_labels("1: task\n3. [diary]\n", 3) == ["task", "thinking", "diary"]


def test_format_chunk_rejects_summary_then_keeps_original():
    calls = []
    def ask(p):
        calls.append(1); return "短"
    assert CC.format_chunk("长" * 200, [], ask) == ["长" * 200] and len(calls) == 2


def test_format_chunk_accepts_filler_removal_above_45pct():
    src = "嗯，" + "内容很多很多" * 30
    out = CC.format_chunk(src, [], lambda p: "内容很多很多" * 30 + "\n\n\n\n第二段")
    assert out[0].startswith("内容") and len(out) == 2


def test_two_pass_labels_per_paragraph():
    def ask(p):
        if "--- 输入开始 ---" in p:
            return "第一段内容要做的事。\n\n第二段是日记。"
        return "1: task\n2: diary"
    md = CC.build("原文", [], [], ask, src_name="x")
    assert "**[task]** 第一段内容要做的事。" in md and "**[diary]** 第二段是日记。" in md


def test_render_same_label_continuation_has_single_blank_line():
    md = CC.render("x", [("task", "甲"), ("task", "乙"), ("diary", "丙")], [], "m")
    assert "**[task]** 甲\n\n乙\n\n**[diary]** 丙" in md and "\n\n\n" not in md


def test_diff_context_for_single_char():
    assert CC.diff_corrections("灵光一线啊", "灵光一闪啊") == [("一线啊", "一闪啊")]


def test_split_chunks_keeps_everything():
    t = "。".join(f"句子{i}" * 20 for i in range(30))
    assert "".join("".join(CC.split_chunks(t, 300)).split()) == "".join(t.split())


def test_diff_corrections():
    c = CC.diff_corrections("基于我的Spike做校验", "基于我的Spec(?)做校验")
    assert c == [("Spike", "Spec")]


# ---------------------------------------------------------------- run(): idempotency, Ollama down
def mk(tmp_path, name="2026-10-16_1000_aha.md", text="Encore 的事情要处理一下", age=1000):
    d = tmp_path / "daily_records"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(text)
    old = time.time() - age
    os.utime(p, (old, old))
    return d, p


def test_creates_derived_in_subdir_and_keeps_source(tmp_path):
    d, p = mk(tmp_path)
    st = CC.run(d, None, ask=echo, up=lambda: True)
    out = d / "enhanced" / "2026-10-16_1000_aha.enhance.md"
    assert st["done"] == 1 and out.exists()
    txt = out.read_text()
    assert "oncall 的事情" in txt and "**[task]**" in txt and "- Encore → oncall" in txt
    assert p.read_text() == "Encore 的事情要处理一下"          # source untouched
    assert not list(d.glob("*.enhance*"))                       # nothing in the dir export_to_context globs


def test_idempotent_and_reprocess_on_change(tmp_path):
    d, p = mk(tmp_path)
    calls = []
    ask = lambda pr: (calls.append(1), echo(pr))[1]
    CC.run(d, None, ask=ask, up=lambda: True)
    n = len(calls)
    st = CC.run(d, None, ask=ask, up=lambda: True)
    assert st["skipped"] == 1 and len(calls) == n               # fresh -> no LLM call
    p.write_text("Encore 的事情要处理一下，又多说了一段话")
    t = time.time() - 500
    os.utime(p, (t, t)); os.utime(CC.out_path(p), (t - 100, t - 100))   # source newer than derived
    st = CC.run(d, None, ask=ask, up=lambda: True)
    assert st["done"] == 1 and len(calls) > n


def test_ollama_down_defers_without_error(tmp_path):
    d, p = mk(tmp_path)
    st = CC.run(d, None, ask=lambda pr: pytest.fail("must not call"), up=lambda: False)
    assert st["ollama_down"] and st["done"] == 0 and not (d / "enhanced").exists()


def test_llm_failure_is_swallowed_and_retried_next_tick(tmp_path):
    d, p = mk(tmp_path)
    def boom(pr): raise ConnectionRefusedError("down")
    st = CC.run(d, None, ask=boom, up=lambda: True)
    assert st["failed"] == 1 and not CC.out_path(p).exists()
    assert CC.run(d, None, ask=echo, up=lambda: True)["done"] == 1


def test_still_growing_file_waits(tmp_path):
    d, p = mk(tmp_path, age=5)
    assert CC.run(d, None, ask=echo, up=lambda: True)["settling"] == 1


def test_day_filter_and_aha2_names(tmp_path):
    d, _ = mk(tmp_path, "2026-10-16_2201_aha-2.md")
    mk(tmp_path, "2026-10-15_2201_aha.md")
    st = CC.run(d, {"2026-10-16"}, ask=echo, up=lambda: True)
    assert st["done"] == 1 and (d / "enhanced" / "2026-10-16_2201_aha-2.enhance.md").exists()


# ---------------------------------------------------------------- nightly digest
@pytest.fixture
def digest(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(DIGEST_DIR))
    sys.modules.pop("aha_digest", None)
    import aha_digest as D
    rec = tmp_path / "daily_records"
    (rec / "enhanced").mkdir(parents=True)
    monkeypatch.setattr(D, "RECORDS", rec)
    monkeypatch.setattr(D, "ENHANCED", rec / "enhanced")
    return D, rec


@pytest.mark.skipif(not DIGEST_DIR.exists(), reason="nightly_digest not present")
def test_digest_glob_includes_aha_dash_n(digest):
    D, rec = digest
    from datetime import date
    (rec / "2026-10-16_1000_aha.md").write_text("a")
    (rec / "2026-10-16_1000_aha-2.md").write_text("b")
    (rec / "2026-10-16_1100_aha.enhance.md").write_text("x")   # not a record name
    names = [p.name for _, p in D.files_in_window(date(2026, 10, 16))]
    assert names == ["2026-10-16_1000_aha-2.md", "2026-10-16_1000_aha.md"] or \
           sorted(names) == ["2026-10-16_1000_aha-2.md", "2026-10-16_1000_aha.md"]


@pytest.mark.skipif(not DIGEST_DIR.exists(), reason="nightly_digest not present")
def test_digest_prefers_derived_and_drops_smalltalk(digest):
    D, rec = digest
    src = rec / "2026-10-16_1000_aha.md"
    src.write_text("原文 Encore 闲聊")
    old = time.time() - 100
    os.utime(src, (old, old))
    (rec / "enhanced" / "2026-10-16_1000_aha.enhance.md").write_text(
        "<!-- x -->\n\n**[task]** oncall 的事\n\n**[smalltalk]** 嗯对\n\n续的碎话\n\n**[thinking]** 想法\n\n## 纠正\n\n- Encore → oncall\n")
    got = D.read_record(src)
    assert "oncall 的事" in got and "想法" in got and "嗯对" not in got and "续的碎话" not in got and "纠正" not in got


@pytest.mark.skipif(not DIGEST_DIR.exists(), reason="nightly_digest not present")
def test_digest_falls_back_when_derived_missing_or_stale(digest):
    D, rec = digest
    src = rec / "2026-10-16_1000_aha.md"
    src.write_text("原文")
    assert D.read_record(src) == "原文"
    u = rec / "enhanced" / "2026-10-16_1000_aha.enhance.md"
    u.write_text("**[task]** 旧版")
    os.utime(u, (time.time() - 500, time.time() - 500))
    assert D.read_record(src) == "原文"


def test_enhance_naming_and_terms(tmp_path):
    assert CC.out_path(Path("/x/2026-10-16_1934_aha.md")) == Path("/x/enhanced/2026-10-16_1934_aha.enhance.md")
    assert CC.out_path(Path("/x/2026-10-16_1651_aha-2.md")).name == "2026-10-16_1651_aha-2.enhance.md"
    ws = tmp_path
    (ws / "tools/kb_catalog").mkdir(parents=True)
    (ws / "tools/kb_catalog/topics.yml").write_text("topics:\n  - name: Doris\n    aliases: [StarRocks, OLAP]\n")
    (ws / "contexts").mkdir()
    (ws / "contexts/catalog.jsonl").write_text('{"topics": ["Aha项目", "Doris"]}\n{"topics": ["Aha项目"]}\n')
    t = CC.build_terms(ws, [("x", "kube-system")], [])
    assert t.startswith("kube-system") and "Doris" in t and "Aha项目" in t and "StarRocks" in t
    assert CC.build_terms(tmp_path / "nowhere", [], []) == ""        # tolerate absence


def test_new_glossary_entries():
    hard, _ = CC.load_glossary()
    out, _ = CC.apply_glossary("Uncore 里 circle 查询，CircleLit 不动，Cube System 和 cubesystem，circles", hard)
    assert out == "oncall 里 SQL 查询，SQLite 不动，kube-system 和 kube-system，circles"


def test_export_stale_glob_skips_enhance(tmp_path):
    import re
    ours = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_aha(-\d+)?\.md$")
    assert not ours.match("2026-10-16_1934_aha.enhance.md")
