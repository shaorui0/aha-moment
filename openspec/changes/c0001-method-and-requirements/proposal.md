## Why

Aha Moment already works as a capture tool (iPhone button -> local Whisper -> one text file per recording). What is missing is the layer that says why it is built this way, what must never be violated, and how a spoken sentence becomes context an agent starts every session with. Without that layer the repo reads as a recording app. With it, the repo is a worked example of context engineering: rules are code, the starting context of each session is deterministic, and accumulation is auditable.

## What Changes

- `docs/REQUIREMENTS.md`: requirements distilled from `PLAN.md` and `README.md`, including the data boundary (what may and may not enter git).
- `docs/METHOD.md`: the model. One flywheel diagram (running parts solid, direction parts dashed), a step table for how one utterance becomes context, the three memory layers, the invariants as hard rules, a failure-mode table, and a section on why this is context engineering.
- `tools/check_rules.py`: leak scan and doc-reference check, plus `.leak_patterns.local` (gitignored) for private patterns.
- `.gitignore`: ignore `.leak_patterns.local`.

## Capabilities

### New Capabilities
- `method-docs`: the model, requirements and invariants are written down and cross-checked.
- `leak-scan`: a deterministic scan that fails when personal data or employer identifiers enter the repo.

### Modified Capabilities
(none)

## Impact

Docs and one tool only. No change to pipeline, server or app behaviour. Files that other processes are editing (`README.md`, `pipeline/*`) are not touched.
