# Aha Moment — iOS app (M3)

SwiftUI, iOS 17+, no third-party dependencies. One-button recording (tap to start, tap to stop) -> 60 s AAC segments -> durable upload queue -> Mac server. One start->stop run = one recording = one text file on the Mac. For a no-Xcode Day-1 path see `SHORTCUT.md`.

Source files in `AhaMoment/`: `AhaMomentApp.swift`, `ContentView.swift`, `CaptureEngine.swift` (session/engine/interruptions), `SegmentWriter.swift` (AAC 16 kHz mono 32 kbps, rotation, sidecar + sha256), `UploadQueue.swift` (background URLSession, ACK-before-delete), `Settings.swift` (UserDefaults + Keychain).

## Setup from zero

1. Install **Xcode** from the Mac App Store (~40 GB unpacked; the Mac has ~63 GB free, so free space first). Open it once, accept the license, let it install components, and add the iOS platform when asked.
2. Point the toolchain at it without sudo: `export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` (what `ops/ios_reinstall.sh` does; xcode-select is left on the Command Line Tools)
3. `brew install xcodegen`
4. Edit `ios/project.yml`: replace `com.CHANGEME.ahamoment` with a unique id, e.g. `com.<yourname>.ahamoment`.
5. `cd projects/aha_moment/ios && xcodegen generate` -> creates `AhaMoment.xcodeproj` (and `AhaMoment/Info.plist`).
   - If you do not want XcodeGen: Xcode > New Project > iOS App (SwiftUI), delete the template files, drag in the 6 `.swift` files, copy the keys from `project.yml` `info.properties` into Info (mic usage, local network usage, `UIBackgroundModes` = `audio`, App Transport Security: Allow Local Networking = YES and Allow Arbitrary Loads = YES), set deployment target 17.0.
6. `open AhaMoment.xcodeproj` -> select target AhaMoment -> **Signing & Capabilities** -> tick "Automatically manage signing" -> Team = your **Personal Team** (sign in with a free Apple ID under Xcode > Settings > Accounts).
7. On the iPhone: **Settings > Privacy & Security > Developer Mode** -> on -> restart -> confirm.
8. Plug the iPhone in (USB, tap Trust), pick it as run destination, press Run. First launch fails with "Untrusted Developer": go to **Settings > General > VPN & Device Management** -> your Apple ID -> Trust. Run again.
9. In the app: Settings -> server defaults to `http://shaoruideMacBook-Pro.local:8765`; put the Mac LAN IP in Backup, token, "Test /v1/health". Allow microphone and Local Network prompts.

**Free provisioning expires every 7 days**: the app stops launching and must be re-run from Xcode (data in the app container survives a re-install over the top; do not delete the app or you lose un-uploaded queue). Max 3 free-signed apps per device. The $99/yr Apple Developer Program removes the 7-day limit (1 year).

Built and running on device since 2026-10-12 (Xcode 26.3, iOS 26.6.1). Day-to-day rebuild/reinstall: `ops/ios_reinstall.sh`; full setup: `docs/SETUP_IPHONE.md`.

## Behaviour notes

- Files: `Application Support/AhaMoment/queue/<id>.aac` (ADTS, crash-safe; older builds `.m4a`) + `<id>.json` (sidecar written last = "ready"). 200/201 -> moved to `recordings/` (kept 7 days for playback); 409 -> `quarantine/`; anything else -> exponential backoff (5 s ... 15 min), network-back (NWPathMonitor) resets backoff. Crash leftovers (audio without sidecar) are salvaged on launch.
- Recordings (Voice-Memos style): one start->stop run = one recording (`session_id` + `session_start` in the sidecar; legacy sidecars without them form one-chunk recordings; old optional fields `session_kind`/`ambient_session_*` still decode but are no longer written). The session id is persisted (`captureDesired` + session keys), so interruption/config-change/relaunch resumes keep it until the user taps stop. All chunks are uploaded with `X-Aha-Source: ambient`. After a 200/201 the chunk moves to `Application Support/AhaMoment/recordings/` (sidecar `uploaded: true`) instead of being deleted; recordings older than 7 days (UserDefaults `recordingRetentionDays`) are pruned on launch. Main screen -> "Recordings": list (newest first, start time/duration/n of m uploaded, cloud button on the left), tap for play/pause + scrubber over an AVMutableComposition of all chunks (queued chunks included; live recording shows "Recording…" and is rebuilt every 5 s). Session category gets `.defaultToSpeaker` so playback is not routed to the earpiece.
- Phone preview transcript (main screen toggle 「手机预转写」, UserDefaults `useOnDeviceAsr`, default on; model `phoneAsrModel` tiny/base/small in Settings, default base ~147 MB, downloaded on first use to `whisperkit/`, automatically only on Wi-Fi): each closed chunk is transcribed on the phone (foreground only; skipped if < 1 s or silent) into `Application Support/AhaMoment/phone/<chunk id>.txt` (pruned with the retention window). Review shows it at once (「手机预转（Mac 校对中…）」); when the Mac text is ready the user is asked 保留手机版 / 用 Mac 校对版, never replaced silently. Review also transcribes missing chunks of fresh / not-yet-uploaded recordings when opened. Upload and audio format are unchanged; the Mac stays authoritative.
- Main screen: one large start/stop toggle (elapsed time while recording), upload-mode picker, Recordings, Settings, Queue. No hold-to-talk, no Ambient/Quick distinction.
- Upload headers: every `PUT /v1/audio/{id}` sends `X-Aha-Session: <session_id>` and `X-Aha-Session-Start: <ISO8601 recording start>` from the sidecar (omitted for legacy chunks without session fields), plus `X-Aha-Priority: high` for priority chunks.
- Upload mode (main screen, UserDefaults `uploadMode`, default `auto`): "自动上传：关" (`record`) keeps capturing but makes no upload attempts, no `/v1/health` probes, no retry timers (chunks accumulate in `queue/`; path changes are ignored too). "自动上传" (`auto`) is the normal behaviour; while the server is unreachable the probe backs off 30 s, 60 s, 2 min, 5 min (cap) globally (not per file) and resets on network change, app foreground, "Retry now", mode switch and "Retry now". Switching to auto kicks the queue immediately.
- Priority upload: Recordings list, cloud button left of each recording -> uploads every pending chunk of that recording first with `X-Aha-Priority: high` (even in record-only mode); if it is being recorded, the running chunk is closed now and recording continues. Icon: cloud-arrow (pending) / pulsing orange (sending) / green check (all uploaded).
- Battery log: `events.log` gets `battery: event=start|periodic|stop level=87% state=unplugged|charging|full mode=auto|record thermal=nominal|fair|serious|critical` on capture start/stop and at most every 10 min from the segment-finished callback (no timer).
- Power: AAC converter output buffer reused, RMS via `vDSP_measqv`, tap bufferSize 8192 (fewer wakeups; format unchanged).
- Server URLs: Settings has a primary URL (default Bonjour name `shaoruideMacBook-Pro.local`) and a backup URL (Mac LAN IP). No Tailscale in v1; off-network segments stay queued. Before uploading, `/v1/health` is probed on the primary URL with a 3 s timeout; if it does not answer, the backup is tried. The chosen base is cached for 60 s.
- Upload retry backoff: 5 s, 10 s, 20 s ... doubling up to 15 min per file; network-back, app foreground and "Retry now" reset it. Server 400/413/415/422 (e.g. sha mismatch): 3 tries, then sha256 is recomputed from the file and 3 more tries, then the file is quarantined.
- Quarantine: files the server will never accept (409 id conflict, repeated 422 after rehash, unreadable orphans) are moved to `Application Support/AhaMoment/quarantine/` with a `<id>.reason.txt`. They are never deleted or retried automatically; the count shows in the UI.
- Capture restart backoff (engine, not upload): after an interruption ends the app retries immediately, then after 2, 4, 8, 16, 32, 60 s (60 s thereafter), at most 10 retries (~6 min), and again on every foreground, route change, config change and media-services reset.
- "Recording stopped" notification: the first time recording is started the app asks for notification permission. If capture is desired-ON but the engine is down (the restart right after an interruption failed, or the 10 retries are exhausted) it posts one local notification, "Aha 录音已停止 — 点按恢复", at most once per stop event (re-armed once capture runs again). Tapping it opens the app; on foreground the app restarts capture automatically. If you deny notifications, nothing is posted.
- Honest limitation: after a phone call or Siri, iOS usually refuses to restart microphone input while the app is in the background (error '!int'), so the automatic restart often fails and the notification is how you find out. Expect a gap until you tap it; see `RESEARCH.md` and tests 4/5.
- `events.log` in the same folder records interruptions/restarts for post-mortems.
- "Skip silent segments" setting (default OFF) drops silent segments with peak RMS < 0.003 before upload. `peak_rms` is always in the sidecar.
- Orange mic dot stays on while capturing: that is iOS's mandatory indicator.

## M3 test checklist

Before each run: set Settings -> Battery -> note the percentage, charge state, and note "Aha Moment" share after the run. Keep the server running; watch `~/aha_data` for arrivals.

| # | Test | Pass criteria |
|---|---|---|
| 1 | Lock screen 30 min, recording ON, phone on desk | ~30 segments on Mac, no gaps (ids/recorded_at contiguous), app still running (orange dot) |
| 2 | Lock screen 2 h | 120 segments, 0 missing, Pending = 0 on unlock |
| 3 | Lock screen 8 h (overnight, on charger first, then once off charger) | same + battery drain recorded |
| 4 | Incoming call: let it ring, answer 30 s, hang up | segment closed at call start; check `events.log` for "interruption ended"; does recording resume WITHOUT opening the app? Record the answer honestly |
| 5 | Siri: invoke Siri mid-recording, ask something | same as above |
| 6 | Airplane mode 10 min then off | recording continues, Pending grows, drains to 0 within ~1 min of reconnect |
| 7 | Kill app (swipe up) while recording | recording stops (iOS limitation); relaunch resumes since desired=ON; no orphan loss (orphan salvage) |
| 8 | Reboot phone | after first unlock, open app once; capture resumes; queue intact |
| 9 | Stop and start again | two recordings in the list, each one uploaded as its own session |
| 10 | Bluetooth headset connect/disconnect mid-run | engine restarts, no crash, `events.log` shows route change |

Reading battery: **Settings > Battery** -> scroll to "Battery Usage by App" -> toggle "Last 10 Days"/"Last 24 Hours" -> tap Aha Moment ("Screen & Home Screen" vs "Background" breakdown). Compare with an idle night. Also check Settings > Cellular / Wi-Fi data for upload volume (~14 MB/h expected).

## Known risks

See the final report in the task hand-off; the main one: if an interruption ends while the app is in the background, iOS may refuse `setActive(true)`/engine start (recording cannot be started from background unless the audio session was kept alive). The app retries with backoff (2 s ... 60 s, up to 10 retries), posts the "recording stopped" notification, and restarts on every foreground, but a gap until you open the app is possible. Test 4/5 measures exactly this.
