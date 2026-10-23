#!/bin/bash
# One recording -> one text file in context-infrastructure contexts/daily_records/, for yesterday (finalize) and today.
# Called by the workspace cron (periodic_jobs/aha_daily_record/run.sh) and by the server after a priority transcription.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"
for D in "$(date -v-1d +%F)" "$(date +%F)"; do
  "$PY" "$HERE/export_to_context.py" --date "$D" --apply
done
# Corrected + classified derived copies (local Ollama). Background; the script holds its own lock, never blocks or fails the export.
# Then extract [task] segments -> inbox/tasks.json for the iPhone Shortcut (deterministic, no LLM).
( "$PY" "$HERE/correct_classify.py" --recent >> /tmp/aha_correct_classify.log 2>&1 || true
  "$PY" "$HERE/extract_tasks.py" >> /tmp/aha_correct_classify.log 2>&1 || true ) &
exit 0
