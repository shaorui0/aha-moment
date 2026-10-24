# Process

How this repository changes, and what has been changed so far. The model is in [METHOD.md](METHOD.md); what the system must do and what may never enter the repo is in [REQUIREMENTS.md](REQUIREMENTS.md).

## How each change goes

Every change is one OpenSpec change (`c<NNNN>-<slug>`), committed in stages: proposal, spec+tasks, impl, docs, acceptance. The first line of each commit is `c<NNNN>-<slug> [k/n <stage>]: <summary>`. Acceptance means a raw `evidence.md` (full `python3 -m pytest -q` output, `python3 tools/check_rules.py`, `openspec validate`), a local HTML report in `reports/`, blog notes in `notes/blog/` (core ideas, traps, counter-intuitive results; not polished), and one row below. Only the files a change created or intentionally edited are staged, because other processes keep uncommitted edits in this tree. The full loop is `.claude/skills/aha-change-loop/SKILL.md`.

## Progress

| Change | Date | What | Takeaway | Report · Notes |
|---|---|---|---|---|
