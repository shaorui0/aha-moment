## 1. Tests
- [x] 1.1 `pipeline/tests/test_export_invariants.py`: text only / no heading, `-2` and `-3`, data root untouched, idempotent second run, fixtures only

## 2. Process
- [x] 2.1 `.claude/skills/aha-change-loop/SKILL.md`
- [x] 2.2 `docs/PROCESS.md` with the Progress table
- [x] 2.3 METHOD and REQUIREMENTS reference the new files

## 3. Acceptance
- [x] 3.1 new tests pass; full `python3 -m pytest -q` output saved raw (failures recorded, tests not edited)
- [x] 3.2 `python3 tools/check_rules.py` prints OK
- [x] 3.3 commit subjects all match the stage pattern; other processes' edits still unstaged
- [x] 3.4 `openspec validate c0002-process-loop-and-evidence`
- [x] 3.5 evidence.md, HTML report, blog notes, Progress rows for c0001 and c0002
