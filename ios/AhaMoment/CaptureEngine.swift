import AVFoundation
import UIKit
import UserNotifications

/// Owns AVAudioSession + AVAudioEngine. One mode: tap start -> continuous recording rotated into chunks -> tap stop.
/// Desired state ("capture should be on") is persisted; every failure path retries while desired.
@MainActor
final class CaptureEngine: ObservableObject {
    @Published private(set) var isOn = false        // desired state
    @Published private(set) var running = false         // engine actually running
    @Published private(set) var segmentCount = 0
    @Published private(set) var startedAt: Date?
    @Published private(set) var lastError: String?
    @Published private(set) var lastEvent = ""
    @Published private(set) var liveSessionIds = Set<String>()   // recordings being written right now

    // One start->stop run = one recording (session). Persisted (Settings.ambientSession) so relaunch/resume keeps it.
    private var session: SessionRef? { didSet { refreshLive() } }

    private let queue: UploadQueue
    private var engine: AVAudioEngine?
    private var writer: SegmentWriter?
    private var retryWork: DispatchWorkItem?
    private var retryAttempt = 0
    private static let maxRetries = 10          // 2,4,8,16,32,60 x5 s ~ 6 min, then give up until next foreground
    private var stopNotified = false            // one local notification per stop event; reset when capture runs again
    private var observers: [NSObjectProtocol] = []
    private var batteryOpen = false             // a capture run is in progress (start logged, stop not yet)
    private var lastBatteryLog = Date.distantPast
    private static let batteryLogInterval: TimeInterval = 600
    private var heartbeat: Timer?               // 30 s main-queue timer, only while `running`
    private var engineStartedAt = Date.distantPast
    private var stopPending = false             // a delayed 'aha.stopped' notification is scheduled
    private var lastDiskWarn = Date.distantPast
    private static let heartbeatInterval: TimeInterval = 30

    init(queue: UploadQueue) {
        self.queue = queue
        UIDevice.current.isBatteryMonitoringEnabled = true
        isOn = Settings.captureDesired
        if isOn {
            session = Settings.ambientSession   // nil -> created at engine start
            startedAt = session?.start ?? Date()   // relaunch/resume: keep timer + waveform visible
        }
        let nc = NotificationCenter.default
        let s = AVAudioSession.sharedInstance()
        func obs(_ name: Notification.Name, _ obj: Any?, _ h: @escaping (CaptureEngine, Notification) -> Void) {
            observers.append(nc.addObserver(forName: name, object: obj, queue: .main) { [weak self] n in
                MainActor.assumeIsolated { if let self { h(self, n) } }
            })
        }
        obs(AVAudioSession.interruptionNotification, s) { $0.onInterruption($1) }
        obs(AVAudioSession.routeChangeNotification, s) { $0.onRouteChange($1) }
        obs(AVAudioSession.mediaServicesWereResetNotification, s) { me, _ in
            me.event("media services reset"); me.restart(reason: "media reset")
        }
        obs(.AVAudioEngineConfigurationChange, nil) { me, n in
            // Only react to our own engine; fires when HW format changes (e.g. BT route) and stops the engine.
            if let e = n.object as? AVAudioEngine, e === me.engine { me.event("engine config change"); me.restart(reason: "config change") }
        }
    }

    // MARK: - public API

    /// Call on launch and on every foreground: starts capture if desired and not running.
    func resumeIfNeeded() {
        guard isOn, !running else { return }
        retryAttempt = 0
        restart(reason: "resume")
    }

    func setRecording(_ on: Bool) {
        Settings.captureDesired = on
        isOn = on
        if on {
            requestNotificationAuth()
            startedAt = Date(); segmentCount = 0
            retryAttempt = 0; stopNotified = false
            session = newSession(); Settings.ambientSession = session
            PhoneASR.shared.warmUp(userInitiated: false)    // model loads while the user talks
            if running { writer?.setSession(session) }
            else { restart(reason: "user start") }
            event("recording on (user)")
            Notify.post("aha.state", "Aha 录音已开启", running ? "正在后台录音" : (lastError ?? "正在启动…"))
            if running { Notify.armWatchdog() }
        } else {
            cancelRetry()
            event("recording off (user)")
            stopPending = false
            UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["aha.stopped"])
            Notify.disarmWatchdog()
            Notify.post("aha.state", "Aha 录音已关闭", "")
            session = nil; Settings.ambientSession = nil
            teardown()
            startedAt = nil
        }
    }

    /// Recordings list cloud button for one recording: if it is being recorded right now, close the running chunk
    /// (flagged priority, recording continues), then upload all its pending chunks first.
    func sendSession(_ id: String) {
        let live = (running && liveSessionIds.contains(id)) ? writer : nil
        live?.rotatePriority()
        queue.sendSession(id, running: live != nil)
    }

    /// One line in events.log for real power data. Called on start/stop and from the segment-finished callback (no timer).
    private func logBattery(_ tag: String) {
        lastBatteryLog = Date()
        let d = UIDevice.current
        let lvl = d.batteryLevel < 0 ? "n/a" : "\(Int((d.batteryLevel * 100).rounded()))%"
        let st: String
        switch d.batteryState {
        case .unplugged: st = "unplugged"
        case .charging: st = "charging"
        case .full: st = "full"
        default: st = "unknown"
        }
        let th: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: th = "nominal"
        case .fair: th = "fair"
        case .serious: th = "serious"
        case .critical: th = "critical"
        @unknown default: th = "unknown"
        }
        appendEvent("battery: event=\(tag) level=\(lvl) state=\(st) lowpower=\(ProcessInfo.processInfo.isLowPowerModeEnabled) mode=\(Settings.uploadMode.rawValue) thermal=\(th)")
    }

    // MARK: - engine lifecycle

    private func newSession() -> SessionRef { let d = Date(); return SessionRef(id: Fmt.newSegmentID(d), start: d) }
    private func refreshLive() { liveSessionIds = Set([session?.id].compactMap { $0 }) }

    private var wantRunning: Bool { isOn }

    private func restart(reason: String) {
        teardown(keepDesired: true)
        guard wantRunning else { return }
        do {
            try startEngine()
            let wasStopped = stopNotified || reason.hasPrefix("interruption") || reason.hasPrefix("retry") || reason == "resume"
            running = true; lastError = nil; retryAttempt = 0; stopNotified = false
            engineStartedAt = Date()
            stopPending = false
            let nc = UNUserNotificationCenter.current()
            nc.removePendingNotificationRequests(withIdentifiers: ["aha.stopped"])
            nc.removeDeliveredNotifications(withIdentifiers: ["aha.stopped", "aha.watchdog"])
            startHeartbeat()
            checkDisk()
            event("started (\(reason))")
            if !batteryOpen { batteryOpen = true; logBattery("start") }
            if isOn {
                Notify.armWatchdog()
                if wasStopped { Notify.post("aha.state", "Aha 录音已恢复", "原因：\(reason)") }
            }
        } catch {
            running = false
            lastError = error.localizedDescription
            event("start failed (\(reason), appState=\(UIApplication.shared.applicationState.rawValue)): \(error.localizedDescription)")
            let ns = error as NSError
            if ns.domain == "aha" && (ns.code == 3 || ns.code == 4) {
                // Mic permission: not retryable (code 3 restarts from the permission callback; resumeIfNeeded retries on foreground).
                if ns.code == 4 { notifyMicDenied() }
                return
            }
            if reason == "interruption ended" { notifyStopped() }   // first failure after a call/Siri: tell the user now
            else if isOn { scheduleDelayedStopped(error.localizedDescription) }   // fires in 45 s unless a restart succeeds
            scheduleRetry()
        }
    }

    private func startEngine() throws {
        let s = AVAudioSession.sharedInstance()
        // Mic permission must be known before starting; undetermined -> ask, then restart from the callback.
        switch AVAudioApplication.shared.recordPermission {
        case .granted: break
        case .undetermined:
            AVAudioApplication.requestRecordPermission { [weak self] ok in
                Task { @MainActor in
                    guard let self else { return }
                    if ok { self.restart(reason: "permission granted") }
                    else { self.lastError = "Microphone permission denied (Settings > Aha > Microphone)"; self.event("mic denied"); self.notifyMicDenied() }
                }
            }
            throw NSError(domain: "aha", code: 3, userInfo: [NSLocalizedDescriptionKey: "waiting for microphone permission"])
        default:
            throw NSError(domain: "aha", code: 4, userInfo: [NSLocalizedDescriptionKey: "microphone permission denied"])
        }
        // .mixWithOthers: Apple DTS says a non-mixable session cannot (re)start I/O from the background while another
        // app owns audio (error '!int' 560557684); a mixable one can. Also never steals focus from music/podcasts.
        try s.setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .allowBluetooth, .defaultToSpeaker])   // defaultToSpeaker: in-app playback must not go to the earpiece
        // Best effort: ask not to be interrupted by system alerts (notification sounds, etc.). Does not stop calls/Siri.
        try? s.setPrefersNoInterruptionsFromSystemAlerts(true)
        try s.setActive(true)
        let e = AVAudioEngine()
        let fmt = e.inputNode.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else {
            throw NSError(domain: "aha", code: 1, userInfo: [NSLocalizedDescriptionKey: "no input format (mic unavailable)"])
        }
        if session == nil { session = newSession(); Settings.ambientSession = session }
        guard let w = SegmentWriter(inputFormat: fmt, segmentSeconds: Settings.segmentSeconds, session: session) else {
            throw NSError(domain: "aha", code: 2, userInfo: [NSLocalizedDescriptionKey: "converter init failed"])
        }
        w.onError = { [weak self] m in
            Task { @MainActor in
                guard let self else { return }
                self.lastError = m
                self.event("writer error: \(m)")
                if self.isOn { Notify.post("aha.writeerr", "Aha 录音写入失败：\(m)", "可能是存储空间不足。点按打开查看。") }
            }
        }
        let q = queue
        w.onFinished = { [weak self] meta in
            PhoneASR.shared.chunkFinalized(meta)    // instant on-device preview for Review; the upload below is unchanged
            Task { @MainActor in
                self?.segmentCount += 1
                if self?.isOn == true && self?.running == true { Notify.armWatchdog() }
                if let me = self, me.batteryOpen, Date().timeIntervalSince(me.lastBatteryLog) >= Self.batteryLogInterval { me.logBattery("periodic") }
            }
            q.kick()
        }
        e.inputNode.installTap(onBus: 0, bufferSize: 8192, format: fmt) { buf, _ in w.append(buf) }
        e.prepare()
        try e.start()
        engine = e; writer = w
    }

    private func teardown(keepDesired: Bool = false) {
        heartbeat?.invalidate(); heartbeat = nil
        if let e = engine {
            e.inputNode.removeTap(onBus: 0)
            e.stop()
        }
        writer?.finish()          // closes current segment -> sidecar -> upload kick
        engine = nil; writer = nil
        running = false
        if !keepDesired {
            if batteryOpen { batteryOpen = false; logBattery("stop") }
            try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        }
    }

    // MARK: - heartbeat / stall detection

    /// Low-frequency (30 s) check, only while running. Re-arms the watchdog only if buffers are really arriving, and
    /// self-heals a silently stalled engine. Independent of segment length / skipSilence / long recordings.
    private func startHeartbeat() {
        heartbeat?.invalidate()
        let t = Timer(timeInterval: Self.heartbeatInterval, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.heartbeatTick() }
        }
        t.tolerance = 5
        RunLoop.main.add(t, forMode: .common)
        heartbeat = t
    }

    /// Recent input levels for the foreground waveform (empty when not recording).
    func levels() -> [Float] { writer?.levels ?? [] }

    private func heartbeatTick() {
        guard running, wantRunning, let w = writer else { return }
        let age = CFAbsoluteTimeGetCurrent() - w.lastAppendAt
        if age < 15 {
            if isOn { Notify.armWatchdog() }
        } else if age > 20 && Date().timeIntervalSince(engineStartedAt) > 20 {
            event("stall: no audio buffers for \(Int(age)) s")
            restart(reason: "stall")
        }
    }

    /// Warn (at most hourly) when free space is too low to record reliably.
    private func checkDisk() {
        guard isOn, Date().timeIntervalSince(lastDiskWarn) > 3600 else { return }
        let v = try? Dirs.root.deletingLastPathComponent().resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        guard let free = v?.volumeAvailableCapacityForImportantUsage, free < 200 * 1024 * 1024 else { return }
        lastDiskWarn = Date()
        let mb = free / (1024 * 1024)
        event("low disk space: \(mb) MB free")
        Notify.post("aha.disk", "Aha 存储空间不足", "仅剩约 \(mb) MB，录音可能写入失败。请清理空间。")
    }

    // MARK: - retry (iOS may refuse to (re)start recording from background; keep trying with backoff)

    private func scheduleRetry() {
        guard wantRunning else { return }
        retryWork?.cancel()
        if retryAttempt >= Self.maxRetries {
            event("retries exhausted after \(retryAttempt) attempts; waiting for foreground")
            notifyStopped()
            return
        }
        retryAttempt += 1
        let delay = min(Double(1 << min(retryAttempt, 6)), 60)   // 2,4,8,16,32,60,60...
        let w = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { guard let self, self.wantRunning, !self.running else { return }; self.restart(reason: "retry \(self.retryAttempt)") }
        }
        retryWork = w
        DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: w)
    }

    private func cancelRetry() { retryWork?.cancel(); retryWork = nil }

    // MARK: - "recording stopped" local notification

    /// Asked at the first start (the system only prompts once; later calls are no-ops).
    private func requestNotificationAuth() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { ok, _ in
            ahaLog.info("notification auth: \(ok)")
        }
    }

    /// Capture is desired-ON but the engine is down. Post at most once per stop event; tapping it opens the app,
    /// and the foreground hook (resumeIfNeeded) restarts capture.
    private func notifyStopped() {
        guard isOn, !running, !stopNotified else { return }
        stopNotified = true
        let c = UNMutableNotificationContent()
        c.title = "Aha 录音已停止 — 点按恢复"
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "aha.stopped", content: c, trigger: nil)) { err in
            if let err { ahaLog.error("notify failed: \(err.localizedDescription, privacy: .public)") }
        }
        event("posted 'recording stopped' notification")
    }

    /// Mic permission denied: not retryable, tell the user right away (same id as notifyStopped; one per stop event).
    private func notifyMicDenied() {
        guard isOn, !stopNotified else { return }
        stopNotified = true
        Notify.post("aha.stopped", "Aha 无法录音：麦克风权限被关闭（设置 > Aha > 麦克风）", "")
        event("posted 'mic permission denied' notification")
    }

    /// Restart failed for a non-interruption reason: schedule 'stopped' for +45 s (survives app freeze); cancelled on a successful start.
    private func scheduleDelayedStopped(_ reason: String) {
        guard !stopPending, !stopNotified else { return }
        stopPending = true
        Notify.post("aha.stopped", "Aha 录音已停止 — 点按恢复", reason, after: 45)
    }

    // MARK: - notifications

    private func onInterruption(_ n: Notification) {
        guard let raw = n.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: raw) else { return }
        switch type {
        case .began:
            event("interruption began")
            // System has stopped the engine. Close the segment so audio so far is safe & uploadable.
            writer?.finish()
            engine?.inputNode.removeTap(onBus: 0)
            engine?.stop()
            engine = nil; writer = nil
            running = false
            heartbeat?.invalidate(); heartbeat = nil
            if isOn {
                // Watchdog stays armed: if .ended never arrives (or the app freezes) it fires ~3 min after the last heartbeat.
                Notify.post("aha.state", "Aha 录音被中断", "来电 / Siri / 其他 App 占用麦克风；结束后会尝试自动恢复")
            }
        case .ended:
            let opts = (n.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt).map(AVAudioSession.InterruptionOptions.init(rawValue:))
            let state = UIApplication.shared.applicationState
            event("interruption ended (shouldResume=\(opts?.contains(.shouldResume) ?? false), appState=\(state.rawValue))")
            // shouldResume is only a hint (absent after e.g. Siri/other-app takeover); our own desired state decides.
            // Try immediately: this is the moment the system is most likely to grant (re)activation from background.
            retryAttempt = 0
            restart(reason: "interruption ended")
        @unknown default: break
        }
    }

    private func onRouteChange(_ n: Notification) {
        guard let raw = n.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
              let reason = AVAudioSession.RouteChangeReason(rawValue: raw), running else { return }
        switch reason {
        case .oldDeviceUnavailable, .newDeviceAvailable, .override, .routeConfigurationChange:
            // Input format may have changed; rebuild tap/converter. (Config-change notification may also fire; restart is idempotent.)
            event("route change \(reason.rawValue)")
            restart(reason: "route change")
        default: break
        }
    }

    private func event(_ s: String) {
        lastEvent = s
        appendEvent(s)
    }
}

/// Local notifications for capture state. The watchdog is a dead-man switch: while recording runs, a notification
/// is kept scheduled ~3 min ahead and pushed back by a 30 s heartbeat that only fires while audio buffers are arriving. If the app is killed, crashes or is
/// jettisoned, nothing pushes it back and it fires. No server/APNs needed.
enum Notify {
    static let watchdogSeconds: TimeInterval = 180

    static func post(_ id: String, _ title: String, _ body: String, after: TimeInterval? = nil) {
        let c = UNMutableNotificationContent()
        c.title = title
        if !body.isEmpty { c.body = body }
        c.sound = .default
        let trigger = after.map { UNTimeIntervalNotificationTrigger(timeInterval: max(1, $0), repeats: false) }
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id, content: c, trigger: trigger)) { err in
            if let err { ahaLog.error("notify failed: \(err.localizedDescription, privacy: .public)") }
        }
    }

    static func armWatchdog() {
        post("aha.watchdog", "Aha 可能已停止录音", "超过 3 分钟没有收到音频（App 被退出、崩溃或卡住）。点按打开以恢复。", after: watchdogSeconds)
    }

    static func disarmWatchdog() {
        UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["aha.watchdog"])
    }
}
