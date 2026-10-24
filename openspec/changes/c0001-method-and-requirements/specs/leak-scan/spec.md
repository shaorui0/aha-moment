## ADDED Requirements

### Requirement: Leak scan
`tools/check_rules.py` SHALL exit 1 and name the file and line when a tracked or untracked non-ignored text file matches a public leak pattern (AWS account id, access key id) or a pattern in `.leak_patterns.local`, or when audio or transcript files are present in the tree.

#### Scenario: Clean repository
- **WHEN** the scan runs on a clean repository
- **THEN** it prints `check_rules: OK` and exits 0

#### Scenario: Leak
- **WHEN** a file contains a line matching a private pattern
- **THEN** the scan prints `FAIL leak pattern in <file>:<line>` and exits 1

### Requirement: Home paths only in legacy config
Absolute home-directory paths SHALL be rejected in documentation, specs, reports, notes and tools; they are tolerated only in launchd plists and code that predates this change.

#### Scenario: New doc with a home path
- **WHEN** a file under `docs/`, `openspec/`, `reports/`, `notes/` or `tools/` contains `/Users/<name>/`
- **THEN** the scan fails
