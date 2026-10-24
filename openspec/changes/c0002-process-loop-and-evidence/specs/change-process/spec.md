## ADDED Requirements

### Requirement: Staged commits
Every change SHALL be committed in stages whose first line matches `c<NNNN>-<slug> [k/n <stage>]: <summary>`.

#### Scenario: History check
- **WHEN** `git log --format=%s` is read for commits of a change
- **THEN** every line matches the pattern

### Requirement: Progress table
The acceptance stage SHALL append one row (Change / Date / What / Takeaway / Report and notes) to the Progress table in `docs/PROCESS.md`.

#### Scenario: Two changes
- **WHEN** c0001 and c0002 are accepted
- **THEN** the table has two rows, each linking its report and notes

### Requirement: Other people's edits are preserved
A change SHALL `git add` only the files it created or intentionally modified.

#### Scenario: Dirty tree
- **WHEN** other uncommitted edits exist in the tree
- **THEN** they remain modified and unstaged after the change's commits
