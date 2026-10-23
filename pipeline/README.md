# Aha pipeline (M4): digest + quick-command dispatch

Stdlib only; run with `projects/aha_moment/.venv/bin/python`. Reads `~/aha_data` (or `$AHA_DATA_DIR`); never touches `server/`.

## Scripts
| Script | What |
|---|---|
| `daily_digest.py --date D [--llm] [--stdout]` | Writes `~/aha_data/digest/D.md`: raw timeline (HH:MM, source tag, `file://` link to item dir). Skips silent/failed/pending/empty. Ambient segments with gap <= 120s (`--gap`) merge into blocks (<= 30 min, `--max-block-min`); quick items are standalone. `--llm` appends a separate "AI-derived" section from `claude -p` (no tools, no session persisted); if it fails the raw digest is still written. Re-run overwrites only that file. |
| `export_to_context.py --date D [--apply]` | One recording (phone start->stop, chunks share `session_id`) -> one text-only file `<workspace>/contexts/daily_records/D_HHMM_aha.md` (`-2` on same-minute clash; stale ones of that pattern removed; skips days without transcripts). Dry-run unless `--apply`. |
| `sync_daily_record.sh` | digest (no LLM) + export `--apply` for yesterday and today. Scheduled by the workspace crontab via `periodic_jobs/aha_daily_record/run.sh` (14:50, 23:55). |
| `correct_classify.py [--recent\|--date D\|--file F\|--all] [--force]` | "Enhance": local Ollama (qwen3:14b, `AHA_CC_MODEL`) + `glossary.txt` + a personal-KB term list (glossary targets, `tools/kb_catalog/topics.yml`, top catalog topics) fixes ASR typos, adds sentence punctuation/paragraph breaks, and labels segments task/thinking/diary/smalltalk. Writes `daily_records/enhanced/<name>_aha.enhance.md` (originals never touched; a subdir because export's stale-cleanup glob would delete a sibling file). Idempotent (skip if derived mtime >= source), skips files written <60s ago, exits 0 if Ollama is down. Run in background at the end of `sync_daily_record.sh` and by nightly_digest `run.sh` every tick. See skill `aha_correct_classify`. |
| `dispatch_quick.py [--dry-run]` | Tails `inbox/quick.jsonl` from the byte offset in `inbox/.cursor`, routes per `routes.json`. |

## Routing (`routes.json`, JSON because PyYAML isn't in the venv)
First match wins; prefix match is case-insensitive, strips `, : ， ： 、` and spaces after the prefix, and ASCII prefixes need a word boundary ("asked" != "ask").
- `待办/todo` -> `inbox/todo.md` (`- [ ] body <!-- id time -->`)
- `记一下/note` -> `inbox/notes.md`
- `问 Claude/ask` -> `claude -p` (no tools) -> `inbox/answers/<id>.md`
- default / empty body -> `inbox/unrouted.md`
Edit the file to add rules (`prefixes` and/or `keywords`, `action: append|ask`).

Delivery semantics: cursor advances per line after the effect; effects are idempotent (id marker in target file, answer file exists), so a crash or lost cursor does not duplicate. Partial trailing line waits for the next run. `flock` blocks overlapping runs. Bad JSON lines go to `inbox/bad_lines.jsonl`. A failed `ask` falls back to `unrouted.md` with an error note (so one bad item cannot wedge the queue).

## Examples
    PY=projects/aha_moment/.venv/bin/python
    $PY projects/aha_moment/pipeline/daily_digest.py --date 2026-10-09 --llm
    $PY projects/aha_moment/pipeline/export_to_context.py --date 2026-10-09          # dry run
    $PY projects/aha_moment/pipeline/export_to_context.py --date 2026-10-09 --apply
    $PY projects/aha_moment/pipeline/dispatch_quick.py --dry-run

## launchd (templates, NOT loaded)
`com.aha.dispatch.plist` (every 120s) and `com.aha.digest.plist` (23:50 daily, `--llm`, today's date; export is not scheduled).

    cp pipeline/com.aha.dispatch.plist pipeline/com.aha.digest.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aha.dispatch.plist
    launchctl bootout gui/$(id -u)/com.aha.dispatch
Logs: `~/Library/Logs/aha-{dispatch,digest}.log`.

## Tests
    projects/aha_moment/.venv/bin/python -m pytest projects/aha_moment/pipeline -q
Uses a temp data dir and a stub `claude` (`AHA_CLAUDE_BIN`); the real CLI is not exercised by tests.
