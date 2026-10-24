## 1. Leak scan
- [x] 1.1 `tools/check_rules.py` (public + local patterns, audio/transcript file check, doc reference check)
- [x] 1.2 `.gitignore` ignores `.leak_patterns.local`

## 2. Documents
- [x] 2.1 `docs/REQUIREMENTS.md` from PLAN.md and README.md, with data boundary
- [x] 2.2 `docs/METHOD.md` (mermaid, step table, memory layers, invariants, failure modes, context-engineering section)

## 3. Acceptance
- [x] 3.1 `python3 tools/check_rules.py` prints OK
- [x] 3.2 A planted leak makes the scan fail (negative test), then is removed
- [x] 3.3 `openspec validate c0001-method-and-requirements`
- [x] 3.4 `evidence.md`, HTML report, blog notes
