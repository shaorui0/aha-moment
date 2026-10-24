# 0001 method and requirements

- Core idea: the recorder is the boring part. The product is what the next agent session starts with. So the doc has to answer "how does a sentence become session context", not "how do I record".
- Writing invariants forced a question per rule: what enforces it? Most had an enforcer (skip-if-exists, sha check, VAD gate, tests). "One button only" had none. It is a convention, and the table says so instead of pretending.
- Counter-intuitive: the observer, the thing I thought closed the loop, ignores `daily_records/` content by design. Voice reaches memory only through triage. The flywheel has a gap exactly where I assumed it was closed. That became requirement R13.
- Drew the line between solid (running) and dashed (direction) in the diagram and in the table. Merging the same idea across days is dashed: nothing detects repeats yet, and repetition is the strongest signal in speech.
- Trap: a leak scan that fails on legacy files is useless on day one. Home paths in launchd plists are legitimate. So the home-path rule applies only to the new method layer, and synthetic TTS benchmark audio is allowed. Scoping the rule made it usable.
- Trap: forward references. METHOD pointed at files created in the next change, and the doc-reference check failed. Fix: do not backtick a path until it exists.
- The existing suite had one red test before I wrote anything, caused by someone else's uncommitted rename. Recording it as-is is the point; editing the test to go green would destroy the evidence.
