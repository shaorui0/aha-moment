# c0002 evidence

## 3.1 export invariants tests, verbose
```
$ python3 -m pytest -q pipeline/tests -k "export" -v
============================= test session starts ==============================
platform darwin -- Python 3.13.3, pytest-9.0.3, pluggy-1.6.0 -- /opt/homebrew/opt/python@3.13/bin/python3.13
cachedir: .pytest_cache
hypothesis profile 'default'
rootdir: <repo>
configfile: pytest.ini
plugins: anyio-4.9.0, asyncio-1.3.0, hypothesis-6.152.9
asyncio: mode=Mode.STRICT, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 115 items / 106 deselected / 9 selected

pipeline/tests/test_correct_classify.py::test_export_stale_glob_skips_enhance PASSED [ 11%]
pipeline/tests/test_export.py::test_one_recording_one_file PASSED        [ 22%]
pipeline/tests/test_export.py::test_stale_removed_and_foreign_kept PASSED [ 33%]
pipeline/tests/test_export_invariants.py::test_text_only_no_heading_no_metadata PASSED [ 44%]
pipeline/tests/test_export_invariants.py::test_same_minute_gets_dash_2_and_dash_3_in_start_order PASSED [ 55%]
pipeline/tests/test_export_invariants.py::test_recording_without_text_gets_no_file PASSED [ 66%]
pipeline/tests/test_export_invariants.py::test_export_never_touches_the_data_root PASSED [ 77%]
pipeline/tests/test_export_invariants.py::test_second_apply_is_a_noop PASSED [ 88%]
pipeline/tests/test_export_invariants.py::test_recording_that_grows_is_rewritten_not_duplicated PASSED [100%]

====================== 9 passed, 106 deselected in 0.15s =======================
```

## 3.1 full suite (raw, unedited tests)
```
$ python3 -m pytest -q
........................................................................ [ 33%]
........................................................................ [ 67%]
.............................................................F.......    [100%]
=================================== FAILURES ===================================
_____________________ test_llm_title_retries_when_too_long _____________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x107619780>

    def test_llm_title_retries_when_too_long(monkeypatch):
        class G:
            calls = []
            @staticmethod
            def chat(model, msgs, opts):
                G.calls.append(msgs[0]["content"]); return {"n": len(G.calls)}
            @staticmethod
            def content(r):
                return "这是一个非常非常非常非常长的待办标题没完没了" if r["n"] == 1 else "改告警规则"
>       assert ET.llm_title(G, "x") == "改告警规则" and len(G.calls) == 2
               ^^^^^^^^^^^^
E       AttributeError: module 'extract_tasks' has no attribute 'llm_title'. Did you mean: 'llm_titles'?

server/tests/test_tasks.py:163: AttributeError
=========================== short test summary info ============================
FAILED server/tests/test_tasks.py::test_llm_title_retries_when_too_long - Att...
1 failed, 212 passed in 13.67s
```

Baseline in c0001 evidence: 206 passed, 1 failed. Now: 212 passed, 1 failed. The 6 added passes are `test_export_invariants.py`. The one failure is unchanged and not caused by this change: `server/tests/test_tasks.py::test_llm_title_retries_when_too_long` calls `extract_tasks.llm_title`; an uncommitted revision of `pipeline/extract_tasks.py` by another process renamed it to `llm_titles`. The test was not edited. Absolute repo paths in the pytest header are replaced with `<repo>` (the method layer must not contain home paths).

## 3.2 leak scan
```
$ python3 tools/check_rules.py
check_rules: OK
exit=0
```

## 3.4 openspec validate
```
Change 'c0002-process-loop-and-evidence' is valid
```

## 3.3 commit subjects match the stage pattern
```
$ git log --format=%s e279e28..HEAD | grep -vcE "^c[0-9]{4}-[a-z0-9-]+ \[[0-9]+/[0-9]+ [a-z+]+\]: "
0
(0 = every commit subject matches)
```

## Other processes' edits still unstaged
```
$ git status --short
 M README.md
 M pipeline/README.md
 M pipeline/correct_classify.py
 M pipeline/export_to_context.py
 M pipeline/extract_tasks.py
 M pipeline/tests/test_correct_classify.py
?? openspec/changes/c0002-process-loop-and-evidence/evidence.md
?? pipeline/prompts/
```
