# c0001 evidence

## 3.1 leak scan on the clean tree
```
$ python3 tools/check_rules.py
check_rules: OK
exit=0
```

## 3.2 negative test: planted leaks make it fail
```
$ python3 tools/check_rules.py   # with notes/_planted.md containing an employer name, a home path and an account id
FAIL leak pattern in notes/_planted.md:3
FAIL leak pattern in notes/_planted.md:1
FAIL home path in notes/_planted.md:2
exit=1
$ python3 tools/check_rules.py   # with a stray .aac in asr/
FAIL audio or transcript file in tree: asr/_planted.aac
exit=1
$ python3 tools/check_rules.py   # planted files removed
check_rules: OK
exit=0
```

## 3.3 openspec validate
```
Change 'c0001-method-and-requirements' is valid
```

## Docs check (DoD parts present)
```
mermaid blocks in METHOD.md: 1
9:## 1. The flywheel
54:## 2. Invariants (hard rules)
71:## 3. How one utterance becomes context
88:## 4. Three layers of memory
98:## 5. What this pipeline defends against
113:## 6. How this proves context engineering
40:## 4. 数据边界（硬约束）
```

## Baseline: existing test suite (raw output, before any new test)
```
$ python3 -m pytest -q
........................................................................ [ 34%]
........................................................................ [ 69%]
.......................................................F.......          [100%]
=================================== FAILURES ===================================
_____________________ test_llm_title_retries_when_too_long _____________________

monkeypatch = <_pytest.monkeypatch.MonkeyPatch object at 0x110604980>

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
1 failed, 206 passed in 13.64s
```

One failure exists and is not caused by this change: `server/tests/test_tasks.py::test_llm_title_retries_when_too_long` calls `extract_tasks.llm_title`, which an in-progress revision of `pipeline/extract_tasks.py` (another process, uncommitted) renamed to `llm_titles`. The test is not edited to make the run green.

## Mermaid check
```
$ mmdc -i flywheel.mmd -o flywheel.svg   # block extracted from docs/METHOD.md
exit 0, svg written
```
