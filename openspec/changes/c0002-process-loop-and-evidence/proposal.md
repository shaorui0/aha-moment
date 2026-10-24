## Why

c0001 wrote the model down. Two things are still missing: (1) a repeatable process so every later change leaves the same trail (proposal, spec, tasks, evidence, report, notes, staged commits), and (2) runnable evidence for the invariant that matters most at the capture/context boundary: one recording becomes one text-only file. The existing export test covers naming and `-2`; it does not assert "no title, no metadata, text only", that an export never touches `~/aha_data`, or that re-running it is stable.

## What Changes

- `.claude/skills/aha-change-loop/SKILL.md`: the change loop, adapted from the finops-agent loop; the acceptance stage appends a row to the Progress table in `docs/PROCESS.md`.
- `docs/PROCESS.md`: Progress table, links to METHOD and REQUIREMENTS, how each change goes.
- `pipeline/tests/test_export_invariants.py`: deterministic fixture-based tests for the export invariants. Uses a temp data root only.
- `docs/METHOD.md` and `docs/REQUIREMENTS.md`: reference the new test and process files.
- Evidence: full pytest output, leak scan output.
- Not changed: `README.md` (another process has uncommitted edits there). A one-line link to `docs/PROCESS.md` is left for the owner to add.

## Capabilities

### New Capabilities
- `export-invariants`: tests that pin the one-recording-one-text-file contract.
- `change-process`: the loop and the Progress table.

### Modified Capabilities
(none)

## Impact

Tests and docs only. No runtime code is modified.
