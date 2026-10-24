# 0002 process loop and evidence

- Core idea: a rule you cannot run is a wish. The most important invariant at the capture/context boundary (one recording = one text-only file) had a happy-path test but nothing for "no heading, no metadata", "the data root is untouched" or "second run is a no-op". Those are now six deterministic tests on a temp directory.
- Counter-intuitive: the strongest test is the negative one. "Export never touches the data root" is a byte-for-byte snapshot of the whole tree before and after, with a dry run and an apply in between. It would catch the day someone makes export clean up after itself.
- Trap: the repo is shared with other processes that keep uncommitted edits. The loop therefore says `git add` only your own files, and the evidence ends with a `git status` that shows the others' edits still unstaged. Without that last check "I did not touch their work" is a claim, not a result.
- Trap: pasting raw pytest output into evidence puts an absolute home path in a file the leak scan guards. The scan caught my own evidence file. Fix: replace the repo root with `<repo>` and say so in the file.
- Trap: running only the export test files fails at collection (`_pipeline_helpers` not importable) while the full run and `pytest pipeline/tests` pass. Test import paths depend on how pytest is invoked. Evidence uses the invocations that work and says which.
- One red test stays red on purpose. It belongs to someone else's in-flight rename. Making it green would mean editing a test I do not own, and it would hide a real signal that their change breaks a caller.
- Process point: the Progress table takes its "Takeaway" from the first bullet of the notes. That makes the first bullet the one that has to be worth reading.
