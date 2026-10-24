# Process

How this repository changes, and what has been changed so far. The model is in [METHOD.md](METHOD.md); what the system must do and what may never enter the repo is in [REQUIREMENTS.md](REQUIREMENTS.md).

## How each change goes

Every change is one OpenSpec change (`c<NNNN>-<slug>`), committed in stages: proposal, spec+tasks, impl, docs, acceptance. The first line of each commit is `c<NNNN>-<slug> [k/n <stage>]: <summary>`. Acceptance means a raw `evidence.md` (full `python3 -m pytest -q` output, `python3 tools/check_rules.py`, `openspec validate`), a local HTML report in `reports/`, blog notes in `notes/blog/` (core ideas, traps, counter-intuitive results; not polished), and one row below. Only the files a change created or intentionally edited are staged, because other processes keep uncommitted edits in this tree. The full loop is `.claude/skills/aha-change-loop/SKILL.md`.

## Progress

| Change | Date | What | Takeaway | Report · Notes |
|---|---|---|---|---|
| c0001-method-and-requirements | 2026-10-24 | METHOD (flywheel, invariants, failure modes), REQUIREMENTS with data boundary, leak scan `tools/check_rules.py` | The recorder is the boring part; the product is what the next session starts with. | [report](../reports/0001-method-and-requirements.html) · [notes](../notes/blog/0001-method-and-requirements.md) |
| c0002-process-loop-and-evidence | 2026-10-24 | aha-change-loop skill, this file, six export-invariant tests, raw pytest evidence | A rule you cannot run is a wish; the export contract is now six deterministic tests. | [report](../reports/0002-process-loop-and-evidence.html) · [notes](../notes/blog/0002-process-loop-and-evidence.md) |
