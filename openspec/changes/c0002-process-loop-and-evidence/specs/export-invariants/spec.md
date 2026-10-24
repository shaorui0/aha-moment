## ADDED Requirements

### Requirement: One recording is one text-only file
Exporting a day SHALL write exactly one file per recording named `YYYY-MM-DD_HHMM_aha.md`, whose content is the transcript text of that recording's chunks in order followed by one newline, with no heading, front matter, timestamp line or markup added.

#### Scenario: Chunks of one recording
- **WHEN** a recording has two chunks with texts "a" and "b"
- **THEN** the file content is exactly "a\nb\n"

### Requirement: Same-minute recordings do not collide
A second recording starting in the same minute SHALL be written as `..._HHMM_aha-2.md`, a third as `-3`.

#### Scenario: Three recordings in one minute
- **WHEN** three recordings start in the same minute
- **THEN** the files are `_aha.md`, `_aha-2.md` and `_aha-3.md`, in start order

### Requirement: Export does not touch the source of truth
Exporting SHALL NOT create, modify or delete any file under the data root.

#### Scenario: Apply twice
- **WHEN** export runs with `--apply` twice on the same day
- **THEN** the data root tree is byte-identical before and after, and the second run reports zero new or changed files

### Requirement: Fixtures only
The tests SHALL use a temporary data root and workspace root and SHALL NOT read `~/aha_data`.

#### Scenario: Isolation
- **WHEN** the tests run on a machine whose home contains real recordings
- **THEN** those recordings are not read
