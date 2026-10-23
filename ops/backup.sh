#!/bin/bash
# Back up ~/aha_data with rsync. DRY-RUN by default; pass --run to actually copy.
# Usage: AHA_BACKUP_DEST=/Volumes/X/aha_backup ops/backup.sh [--run]
#   or:  ops/backup.sh --dest user@host:/path [--run]
# No --delete: files removed locally stay in the backup (audio is source of truth).
set -euo pipefail
SRC="${AHA_DATA_DIR:-$HOME/aha_data}/"
DEST="${AHA_BACKUP_DEST:-}"
RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --run) RUN=1 ;;
    --dest) DEST="$2"; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$DEST" ] || { echo "set AHA_BACKUP_DEST or --dest" >&2; exit 2; }
OPTS=(-a --human-readable --stats --exclude '.tmp/')
if [ "$RUN" = 1 ]; then echo "[backup] REAL RUN $SRC -> $DEST"; else echo "[backup] DRY RUN (add --run to copy) $SRC -> $DEST"; OPTS+=(-n -v); fi
exec rsync "${OPTS[@]}" "$SRC" "$DEST/"
