## ADDED Requirements

### Requirement: Method document
`docs/METHOD.md` SHALL contain at least one mermaid diagram of the flywheel, a step table (input / action / criterion / artifact), a failure-mode table, and a section "How this proves context engineering".

#### Scenario: Reader checks the model
- **WHEN** a reader opens `docs/METHOD.md`
- **THEN** each of the four parts is present and parts that are running are distinguished from parts that are direction

### Requirement: Invariants are hard rules
The method document SHALL state these as rules, each with the mechanism that enforces it: audio is the source of truth; transcripts are recomputable and never overwritten; the phone does not delete before server ACK; one recording is one file containing only text; one button is the only entry; LLM steps read enhanced/transcribed text only; personal data does not enter git.

#### Scenario: Invariant has an enforcer
- **WHEN** an invariant is listed
- **THEN** the same row names the code, test or gitignore rule that enforces it, or says it is a convention

### Requirement: Requirements carry the data boundary
`docs/REQUIREMENTS.md` SHALL list what may not enter the repository (audio, transcripts, `daily_records` content, employer identifiers, device-management details).

#### Scenario: Boundary is checkable
- **WHEN** `python3 tools/check_rules.py` runs
- **THEN** it enforces the machine-checkable part of that list
