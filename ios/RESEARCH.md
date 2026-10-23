# iOS research notes (2026-10-09)

Confidence labels: [A] = stated by an Apple DTS/engineer or Apple docs page I fetched; [F] = developer forum report only; [?] = not verified.

## a. Restarting recording from the background after an interruption

Findings
- [A] Apple engineer (Jan 2025): "In general, it is not possible to start a recording from the background." Suggested `AudioRecordingIntent` (App Intents) if recording must start/continue outside the foreground. https://developer.apple.com/forums/thread/771457
  - I could not fetch the AudioRecordingIntent doc body; availability (believed iOS 18+) and exact semantics are [?]. https://developer.apple.com/documentation/appintents/audiorecordingintent
- [A] Apple DTS on resuming after an interruption in the background: error 560557684 ('!int') means another app holds an active session and ours is non-mixable. Options: (1) make the session mixable (`.mixWithOthers`) if it must start from background; (2) stay non-mixable and deactivate on background / reactivate on foreground; (3) don't resume in background, remember intent and resume on foreground. `shouldResume` does not guarantee you can start I/O while backgrounded. https://developer.apple.com/forums/thread/652395 (this thread is about playback; applying it to recording is my inference)
- [F] Jan 2026 report: recording via AVAudioRecorder, UIBackgroundModes=audio, phone call ends, `.ended` arrives, `setActive(true)` fails with '!int' every time, even with retries/new recorder; works only after the app is brought to the foreground. No replies. https://developer.apple.com/forums/thread/813278
- [F] 2019 iOS 13 thread: background recording can only be (re)started after an interruption if it was already active before; a cited Apple-suggested workaround was "keep recording continuously and discard audio" (user considered it too costly in battery). CallKit claims conflicting. https://developer.apple.com/forums/thread/120038?page=3
- [A] Apple DTS (2026, iOS 26.5 thread): keep `.playAndRecord`/`.default` (or record-only) + `audio` background mode; activate the session in the foreground only; avoid frameworks that auto-activate/deactivate the session; infinite `backgroundTimeRemaining` is expected while the session is active; Apple's "Capturing stereo audio from built-in microphones" sample records fine in background. https://developer.apple.com/forums/thread/826462
- [?] `setPrefersNoInterruptionsFromSystemAlerts(_:)`: I could not retrieve its doc body. From memory it only suppresses system-alert interruptions (e.g. notification sounds), not phone calls/Siri. https://developer.apple.com/documentation/avfoundation/avaudiosession/setprefersnointerruptionsfromsystemalerts(_:)
- [F] Siri-specific: `.ended` may arrive only when Siri's card is dismissed. https://developer.apple.com/forums/thread/849429 (title only; not read)

Conclusion / strategy chosen (no Apple guarantee exists; test 4/5 in README decide)
1. Session category `.playAndRecord` + `.mixWithOthers` (+ `.allowBluetooth`), so other apps' audio rarely interrupts at all and activation from background has the best chance.
2. Never deactivate the session while ambient is desired (only on explicit stop). Activate in the foreground when the user starts capture.
3. On `.began`: close the segment (data safe), drop the stopped engine. On `.ended`: ignore `shouldResume` as a gate; immediately try to rebuild session + engine (best chance is right at the end event), then retry with backoff 2..60 s, and again on every foreground (`scenePhase == .active`), route change, config change, media-services reset.
4. Log appState + result of each attempt to `events.log` so the gap is measurable.
5. Residual risk: if iOS refuses, there is a permanent gap until the user opens the app. Possible mitigations NOT implemented: local notification "recording stopped, tap to resume"; `AudioRecordingIntent` + Live Activity button / Action Button (iOS 18+, unverified); keeping a never-stopping recording is impossible during a call.

## b. Shortcuts action names

- `Get Contents of URL` = `获取 URL 内容`: [A] https://support.apple.com/zh-cn/guide/shortcuts/apd58d46713f/ios and English https://support.apple.com/guide/shortcuts/get-contents-of-url-apd58d46713f/ios . Methods GET/POST/PUT/PATCH/DELETE under "Show More"; Request Body (JSON / Form / File) shows only for POST/PUT/PATCH.
- `Show Result` = `显示结果`: [F] https://sspai.com/post/104573
- `Copy to Clipboard`: Chinese string [?]. An sspai iOS 14 article calls it `拷贝至剪贴板` (https://sspai.com/post/66136, via search snippet); SHORTCUT.md says `拷贝到剪贴板` and notes the variant.
- `Record Audio` = `录制音频`, `Text` = `文本`, and the Record Audio options (Quality / Start Recording / Finish Recording): [?] not confirmed from any fetched source; third-party shortcuts confirm "tap to start/stop" recorders exist (https://matthewcassinelli.com/shortcuts/folders/content-types/audio/). The user should verify on the device by searching the action list.
- SHORTCUT.md edited to state which names are verified and which are not, and to mention "Show More".

## c. Battery cost of continuous mic recording

- No controlled measurement of AVAudioEngine tap vs AVAudioRecorder power was found. Both sit on the same audio I/O unit; the extra cost of the engine path is our own conversion/AAC encode CPU work. [?] no source.
- Only data point: 2019 forum report (iOS 12, with a Bluetooth accessory, always-on recording): iPhone 6S Plus 62 h -> 41 h standby (-34%), iPhone 7 about halved. Old hardware and not comparable to current phones. Found via search snippet of https://developer.apple.com/forums/thread/120038?page=3 ; I did not verify the number on the page itself.
- A Firefox iOS bug linked many hours of background activity to an AVAudioEngine left running: https://bugzilla.mozilla.org/show_bug.cgi?id=1220378
- Conclusion: expect a measurable drain; there is no published figure for modern iPhones. The README M3 test (Settings > Battery per-app) is the only real answer. Choice kept: AVAudioEngine tap (needed for 16 kHz conversion, silence stats, gapless rotation). Reduce cost: tap bufferSize 4096 is already large; 32 kbps AAC is cheap; keep screen off.
