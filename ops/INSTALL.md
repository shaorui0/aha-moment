# Aha server install (Mac)

Deps (once): `cd projects/aha_moment && .venv/bin/pip install fastapi uvicorn python-multipart`

## Run in foreground
    projects/aha_moment/.venv/bin/python projects/aha_moment/ops/aha serve
First run generates the token at `~/.aha/token` (0600). Show it: `... ops/aha token`.
Env: `AHA_DATA_DIR` (default `~/aha_data`), `AHA_TOKEN` (overrides file), `AHA_HOST`, `AHA_PORT` (8765).

## launchd (NOT loaded automatically)
    cp ops/com.aha.server.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aha.server.plist   # load
    launchctl kickstart -k gui/$(id -u)/com.aha.server                             # restart
    launchctl bootout gui/$(id -u)/com.aha.server                                  # unload
Logs: `~/Library/Logs/aha-server.log` (`tail -f`). Edit paths in the plist if the repo moves.

## Find the LAN IP for the phone
    ipconfig getifaddr en0
Phone uses `http://<ip>:8765`, header `Authorization: Bearer <token>`.
Check: `curl http://<ip>:8765/v1/health`

## API quick reference
- `PUT /v1/audio/{id}` raw body; headers X-Aha-Sha256 (req), X-Aha-Recorded-At, X-Aha-Source (ambient|quick), X-Aha-Duration-Ms. 201 / 200 exists / 409 / 422 / 401
- `POST /v1/quick[?wait=1]` multipart file or raw body, no id/sha; wait=1 blocks <=60s and returns text
- `GET /v1/status`, `GET /v1/items/{id}` (auth), `GET /v1/health` (open)
- `GET /v1/items?date=YYYY-MM-DD` (auth) list with transcripts; `GET /v1/items/{id}/audio` (Bearer, or `?t=<token>`); `POST /v1/items/{id}/retry`
- `GET /` web UI (open page; enter token once, stored in browser localStorage)

## CLI
`aha status | token | today [--date D] | reprocess --since YYYY-MM-DD --backend X | doctor | retry <id>`
`aha doctor` checks launchd, health, disk (<10GB warns), queue, failed count, last item age, aha_asr import, Tailscale. Exit 1 only on FAIL.
ASR failures: 3 attempts per run, 6 lifetime (stored in meta `lifetime_attempts`); then `failed_permanent` and restarts no longer retry it. `aha retry <id>` resets and requeues.
(`reprocess` runs ASR in its own process; avoid running it while the server is mid-backlog on the same items.)

## Web UI
Open `http://localhost:8765/` (or the phone-reachable URL below). Paste the token (`aha token`) once. Lists a day's items, plays audio, and has a "Quick record" button (MediaRecorder -> `POST /v1/quick?wait=1`).
Microphone access requires a secure context: **it works only via `http://localhost` or HTTPS (Tailscale `serve`)**. iOS Safari will not give mic access over plain `http://<lan-ip>` or `http://100.x`; listing/playback still work there.
The token in `?t=` for audio ends up in URLs/logs, acceptable on a private tailnet only.

## Phone access via Tailscale
1. Install Tailscale on Mac and iPhone, same tailnet. Mac status: `/Applications/Tailscale.app/Contents/MacOS/Tailscale status` (or `tailscale status`).
2. Phone reaches the Mac at the MagicDNS name (`http://<mac-name>.<tailnet>.ts.net:8765`) or the `100.x.y.z` IP (`tailscale ip -4`). Works off the home Wi-Fi; uploads queue offline on the phone.
3. Optional HTTPS (needed for mic in the web UI): enable HTTPS certificates + MagicDNS in the Tailscale admin console, then
       tailscale serve --bg 8765
   and use `https://<mac-name>.<tailnet>.ts.net/`. Undo: `tailscale serve reset`. (Not run/verified by this install; Tailscale was not installed when written.)
4. First time python listens on 0.0.0.0, macOS shows "Do you want python to accept incoming network connections?" -> Allow. If dismissed, fix in System Settings > Network > Firewall > Options. With `tailscale serve` the server could bind `AHA_HOST=127.0.0.1` instead and avoid the prompt (but then the plain-HTTP 100.x path is gone).

## Backup
    AHA_BACKUP_DEST=/Volumes/X/aha_backup ops/backup.sh          # dry run (default)
    AHA_BACKUP_DEST=/Volumes/X/aha_backup ops/backup.sh --run    # real copy
`--dest user@host:/path` also works. No `--delete`; `.tmp/` excluded.
