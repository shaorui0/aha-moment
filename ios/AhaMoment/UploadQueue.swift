import Foundation
import Network
import UIKit

struct PendingItem: Identifiable {
    let id: String
    let bytes: Int
    let source: String
    let attempts: Int
}

/// Durable upload queue. The directory IS the queue: `<id>.aac` + `<id>.json` (sidecar) in Application Support.
/// A file is deleted only after the server answers 200/201. Uses a background URLSession so uploads survive
/// app suspension/termination.
final class UploadQueue: NSObject, ObservableObject, URLSessionDelegate, URLSessionTaskDelegate {
    static let shared = UploadQueue()

    @Published private(set) var pending: [PendingItem] = []
    @Published private(set) var quarantined = 0
    @Published private(set) var lastError: String?
    @Published private(set) var lastSuccess: Date?
    @Published private(set) var priorityStatus: String?     // feedback for the "立即发送" button

    var bgCompletion: (() -> Void)?

    private let q = DispatchQueue(label: "aha.upload")           // guards the mutable state below
    private var session: URLSession!
    private var inFlight = Set<String>()
    private var attempts = [String: Int]()
    private var notBefore = [String: Date]()
    private var wake: DispatchWorkItem?
    private let monitor = NWPathMonitor()
    private var online = true
    private let maxInFlight = 4
    private var cachedBase: (url: URL, at: Date)?
    private var probing = false
    // Probe backoff while the server is unreachable: 30 s, 60 s, 2 min, 5 min (cap). Reset on network change / foreground / retry / mode switch.
    private static let probeBackoff: [TimeInterval] = [30, 60, 120, 300]
    private var probeFailures = 0
    private var probeNotBefore = Date.distantPast
    // "立即发送" bookkeeping (state queue): ids still to be ACKed, total marked, grace window for the rotated chunk to finalize.
    private var priorityOutstanding = Set<String>()
    private var priorityTotal = 0
    private var prioritySendActive = false
    private var priorityGraceUntil = Date.distantPast
    private var priorityFailNotified = false
    private let probeSession: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 3
        c.timeoutIntervalForResource = 4
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()
    // Persisted so "never retry forever" also holds across app relaunches.
    private static let badKey = "aha.badCounts"
    private static let rehashKey = "aha.rehashed"
    private static let maxBad = 3

    private override init() {
        super.init()
        Dirs.prepare()
        let cfg = URLSessionConfiguration.background(withIdentifier: "aha.moment.upload")
        cfg.sessionSendsLaunchEvents = true
        cfg.waitsForConnectivity = true
        cfg.isDiscretionary = false
        cfg.timeoutIntervalForResource = 600   // stale/unreachable base URL fails instead of waiting 7 days, so fallback can kick in
        cfg.allowsCellularAccess = true
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
        session.getAllTasks { [weak self] tasks in
            self?.q.async {
                for t in tasks { if let id = t.taskDescription { self?.inFlight.insert(id) } }
                self?.scanLocked()
            }
        }
        monitor.pathUpdateHandler = { [weak self] path in
            guard let self else { return }
            self.q.async {
                let nowOnline = path.status == .satisfied
                if self.online != nowOnline { self.resetBackoffLocked() }   // path changed: retry immediately
                self.online = nowOnline
                // Record-only mode: stay quiet on path changes unless a priority send is waiting.
                if nowOnline && (Settings.uploadMode == .auto || !self.priorityOutstanding.isEmpty) { self.scanLocked() }
            }
        }
        monitor.start(queue: DispatchQueue(label: "aha.netmon"))
    }

    /// Trigger a scan (segment closed, app foregrounded, settings changed).
    func kick(resetBackoff: Bool = false) {
        q.async { if resetBackoff { self.resetBackoffLocked() }; self.scanLocked() }
    }

    private func resetBackoffLocked() {
        notBefore.removeAll(); probeFailures = 0; probeNotBefore = .distantPast
    }

    /// Upload mode was switched in the UI (the value itself lives in UserDefaults).
    func modeChanged() {
        q.async {
            appendEvent("upload mode: \(Settings.uploadMode.rawValue)")
            self.resetBackoffLocked()
            self.scanLocked()   // auto: kicks the queue now; record: cancels pending wake
        }
    }

    /// Recordings list cloud button: upload every pending chunk of one recording (session) first, even in record-only
    /// mode.
    func sendSession(_ sid: String, running: Bool) {
        q.async {
            let ids = Set(self.pendingMetasLocked().filter { $0.0.sessionId == sid }.map { $0.0.id })
            self.markPriorityLocked(ids, running: running, why: "session \(sid)")
        }
    }

    /// Recordings list "发给 Claude Code": tag one recording (session). Queued chunks carry X-Aha-Tags on upload; chunks
    /// already on the Mac are tagged via POST /v1/sessions/<id>/tags, retried until ACKed (survives offline/relaunch).
    func tagSession(_ sid: String) {
        q.async {
            Settings.claudeSessions.insert(sid)
            Settings.claudeTagPending.insert(sid)
            for (m, side) in self.pendingMetasLocked() where m.sessionId == sid && !(m.tags ?? []).contains("claude") {
                var mm = m; mm.tags = (m.tags ?? []) + ["claude"]
                if let d = try? JSONEncoder().encode(mm) { try? d.write(to: side, options: .atomic) }
            }
            appendEvent("claude tag: session \(sid)")
            self.flushTagsLocked()
        }
    }

    private var tagRetryScheduled = false

    private func flushTagsLocked() {
        let pend = Settings.claudeTagPending
        guard !pend.isEmpty else { return }
        if !tagRetryScheduled {      // keep retrying (server down / chunks not uploaded yet -> 404) until ACKed
            tagRetryScheduled = true
            q.asyncAfter(deadline: .now() + 60) { [weak self] in self?.tagRetryScheduled = false; self?.flushTagsLocked() }
        }
        let bases = Settings.baseURLs(), token = Settings.token
        guard online, !bases.isEmpty, !token.isEmpty else { return }
        resolveBase(bases, token: token) { [weak self] base in
            guard let self, let base else { return }
            for sid in pend {
                var req = URLRequest(url: base.appendingPathComponent("v1/sessions/\(sid)/tags"))
                req.httpMethod = "POST"
                req.timeoutInterval = 10
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                req.setValue("application/json", forHTTPHeaderField: "Content-Type")
                req.httpBody = try? JSONSerialization.data(withJSONObject: ["tags": ["claude"]])
                self.probeSession.dataTask(with: req) { [weak self] _, resp, _ in
                    guard (resp as? HTTPURLResponse)?.statusCode == 200 else { return }   // 404: nothing uploaded yet; retry later
                    self?.q.async { Settings.claudeTagPending.remove(sid) }
                }.resume()
            }
        }
    }

    private func markPriorityLocked(_ ids: Set<String>, running: Bool, why: String) {
        for (m, side) in pendingMetasLocked() where ids.contains(m.id) && m.priority != true {
            var mm = m; mm.priority = true
            if let d = try? JSONEncoder().encode(mm) { try? d.write(to: side, options: .atomic) }
        }
        for id in ids { notBefore[id] = nil }
        probeFailures = 0; probeNotBefore = .distantPast
        priorityOutstanding.formUnion(ids)
        priorityTotal = priorityOutstanding.count + (running ? 1 : 0)
        prioritySendActive = true
        priorityFailNotified = false
        priorityGraceUntil = Date().addingTimeInterval(running ? 4 : 0)   // rotated chunk is finalized asynchronously
        appendEvent("priority send (\(why)): marked \(ids.count) pending, running=\(running)")
        setPriorityStatus("正在发送…")
        scanLocked()
        q.asyncAfter(deadline: .now() + 4.5) { [weak self] in self?.checkPriorityDoneLocked() }
    }

    private func setPriorityStatus(_ s: String?) { DispatchQueue.main.async { self.priorityStatus = s } }

    private func checkPriorityDoneLocked() {
        guard prioritySendActive, priorityOutstanding.isEmpty, Date() >= priorityGraceUntil else { return }
        prioritySendActive = false
        setPriorityStatus(priorityTotal > 0 ? "已发送 \(priorityTotal) 段，Mac 正在转写" : "没有待发送的录音")
    }

    /// Local notification, only on failure of a user-requested priority send (once per request).
    private func priorityFailedLocked(_ why: String) {
        guard prioritySendActive, !priorityFailNotified else { return }
        priorityFailNotified = true
        setPriorityStatus("发送失败：\(why)（已排队，可达时自动重试）")
        Notify.post("aha.priorityFail", "Aha 立即发送失败", why)
    }

    private func pendingMetasLocked() -> [(SegmentMeta, URL)] {
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: Dirs.queue.path)) ?? []).filter { $0.hasSuffix(".json") }.sorted()
        var out: [(SegmentMeta, URL)] = []
        for n in names {
            let side = Dirs.queue.appendingPathComponent(n)
            guard let d = try? Data(contentsOf: side), let m = try? JSONDecoder().decode(SegmentMeta.self, from: d) else { continue }
            out.append((m, side))
        }
        return out
    }

    /// Salvage unclosed audio left by a crash/kill. Call only while capture is not running.
    func recoverOrphans() {
        q.async {
            let fm = FileManager.default
            let files = (try? fm.contentsOfDirectory(at: Dirs.queue, includingPropertiesForKeys: nil)) ?? []
            for a in files where Dirs.audioExts.contains(a.pathExtension) {
                let side = a.deletingPathExtension().appendingPathExtension("json")
                if fm.fileExists(atPath: side.path) { continue }
                if let m = SegmentWriter.recoverOrphan(audio: a),
                   let d = try? JSONEncoder().encode(m) {
                    try? d.write(to: side, options: .atomic)
                    ahaLog.info("recovered orphan \(m.id)")
                } else {
                    self.quarantineLocked(a, reason: "orphan without sidecar, unreadable")
                }
            }
            self.scanLocked()
        }
    }

    // MARK: - scan & upload (state queue)

    private func scanLocked() {
        flushTagsLocked()
        let fm = FileManager.default
        let names = ((try? fm.contentsOfDirectory(atPath: Dirs.queue.path)) ?? []).filter { $0.hasSuffix(".json") }.sorted()
        var items: [(SegmentMeta, URL)] = []
        for n in names {
            let side = Dirs.queue.appendingPathComponent(n)
            guard let d = try? Data(contentsOf: side), let m = try? JSONDecoder().decode(SegmentMeta.self, from: d) else { continue }
            let audio = Dirs.audio(in: Dirs.queue, id: m.id)
            guard fm.fileExists(atPath: audio.path) else { try? fm.removeItem(at: side); continue }
            items.append((m, audio))
        }
        let snapshot = items.map { PendingItem(id: $0.0.id, bytes: $0.0.bytes, source: $0.0.source, attempts: attempts[$0.0.id] ?? 0) }
        let qCount = (try? fm.contentsOfDirectory(atPath: Dirs.quarantine.path).filter { Dirs.audioExts.contains(($0 as NSString).pathExtension) }.count) ?? 0
        DispatchQueue.main.async { self.pending = snapshot; self.quarantined = qCount }

        guard online, !items.isEmpty else { return }
        let bases = Settings.baseURLs()
        guard !bases.isEmpty else { setError("Server URL not set"); return }
        let token = Settings.token
        guard !token.isEmpty else { setError("Token not set"); return }

        // Record-only: only priority chunks may leave the phone. Nothing else -> no probe, no timer, no URLSession task.
        let auto = Settings.uploadMode == .auto
        if prioritySendActive { for (m, _) in items where m.priority == true { priorityOutstanding.insert(m.id) } }
        let eligible = auto ? items : items.filter { $0.0.priority == true }
        guard !eligible.isEmpty else { wake?.cancel(); wake = nil; return }

        let now = Date()
        var nextWake: Date?
        var due: [(SegmentMeta, URL)] = []
        for (m, audio) in eligible {
            if inFlight.contains(m.id) { continue }
            if let nb = notBefore[m.id], nb > now { nextWake = min(nextWake ?? nb, nb); continue }
            due.append((m, audio))
        }
        due.sort { ($0.0.priority == true ? 0 : 1, $0.0.recordedAt) < ($1.0.priority == true ? 0 : 1, $1.0.recordedAt) }   // priority first
        let cachedOK = cachedBase.map { bases.contains($0.url) && now.timeIntervalSince($0.at) < 60 } ?? false
        if !due.isEmpty && !cachedOK && probeNotBefore > now {      // server was unreachable: do not probe before the backoff ends
            nextWake = min(nextWake ?? probeNotBefore, probeNotBefore)
            due = []
        }
        wake?.cancel(); wake = nil
        if let nw = nextWake {
            let w = DispatchWorkItem { [weak self] in self?.scanLocked() }
            wake = w
            q.asyncAfter(deadline: .now() + max(1, nw.timeIntervalSinceNow), execute: w)
        }
        guard !due.isEmpty, inFlight.count < maxInFlight else { return }

        resolveBase(bases, token: token) { [weak self] base in
            guard let self else { return }
            guard let base else {
                let delay = Self.probeBackoff[min(self.probeFailures, Self.probeBackoff.count - 1)]
                self.probeFailures += 1
                self.probeNotBefore = Date().addingTimeInterval(delay)
                self.setError("No server reachable (tried \(bases.map { $0.absoluteString }.joined(separator: ", "))); next probe in \(Int(delay)) s")
                if due.contains(where: { $0.0.priority == true }) { self.priorityFailedLocked("Mac 不可达") }
                self.wake?.cancel()
                let w = DispatchWorkItem { [weak self] in self?.scanLocked() }
                self.wake = w
                self.q.asyncAfter(deadline: .now() + delay, execute: w)
                return
            }
            self.probeFailures = 0; self.probeNotBefore = .distantPast
            for (m, audio) in due {
                if self.inFlight.count >= self.maxInFlight { break }
                if self.inFlight.contains(m.id) { continue }
                var req = URLRequest(url: base.appendingPathComponent("v1/audio/\(m.id)"))
                req.httpMethod = "PUT"
                req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
                req.setValue(m.sha256, forHTTPHeaderField: "X-Aha-Sha256")
                req.setValue(m.recordedAt, forHTTPHeaderField: "X-Aha-Recorded-At")
                req.setValue(m.source, forHTTPHeaderField: "X-Aha-Source")
                req.setValue(String(m.durationMs), forHTTPHeaderField: "X-Aha-Duration-Ms")
                if let sid = m.sessionId {   // legacy chunks without session fields: omit both headers
                    req.setValue(sid, forHTTPHeaderField: "X-Aha-Session")
                    if let st = m.sessionStart { req.setValue(st, forHTTPHeaderField: "X-Aha-Session-Start") }
                }
                if let t = m.tags, !t.isEmpty { req.setValue(t.joined(separator: ","), forHTTPHeaderField: "X-Aha-Tags") }
                if m.priority == true { req.setValue("high", forHTTPHeaderField: "X-Aha-Priority") }
                req.setValue(Dirs.contentType(audio), forHTTPHeaderField: "Content-Type")
                let task = self.session.uploadTask(with: req, fromFile: audio)
                task.taskDescription = m.id
                self.inFlight.insert(m.id)
                task.resume()
            }
        }
    }

    /// Pick the first reachable base URL (primary, then backup). Any HTTP response to GET /v1/health counts as
    /// reachable (auth is checked on upload). Result cached 60 s; invalidated on network errors. Runs on state queue.
    private func resolveBase(_ bases: [URL], token: String, _ done: @escaping (URL?) -> Void) {
        if let c = cachedBase, bases.contains(c.url), Date().timeIntervalSince(c.at) < 60 { done(c.url); return }
        if probing { return }          // a probe is running; its completion triggers a rescan
        probing = true
        let probe = probeSession
        Task.detached { [weak self] in
            var found: URL?
            for b in bases {
                var r = URLRequest(url: b.appendingPathComponent("v1/health"))
                r.timeoutInterval = 3
                if let (_, resp) = try? await probe.data(for: r), resp is HTTPURLResponse { found = b; break }
            }
            self?.q.async {
                guard let self else { return }
                self.probing = false
                self.cachedBase = found.map { ($0, Date()) }
                done(found)
            }
        }
    }

    private func defer_(_ id: String, seconds: TimeInterval) {
        let t = Date().addingTimeInterval(seconds)
        if (notBefore[id] ?? .distantPast) < t { notBefore[id] = t }
    }

    private var lastLoggedError: String?
    private func setError(_ s: String?) {
        if let s, s.hasPrefix("No server reachable"), lastLoggedError?.hasPrefix("No server reachable") == true { lastLoggedError = s; DispatchQueue.main.async { self.lastError = s }; return }   // backoff countdown text changes; log once
        if let s, s != lastLoggedError { appendEvent("upload: \(s)") }
        lastLoggedError = s
        DispatchQueue.main.async { self.lastError = s }
    }

    private func quarantineLocked(_ audio: URL, reason: String = "unknown") {
        let fm = FileManager.default
        let side = audio.deletingPathExtension().appendingPathExtension("json")
        for u in [audio, side] where fm.fileExists(atPath: u.path) {
            let dest = Dirs.quarantine.appendingPathComponent(u.lastPathComponent)
            try? fm.removeItem(at: dest)
            try? fm.moveItem(at: u, to: dest)
        }
        let id = audio.deletingPathExtension().lastPathComponent
        let note = "\(Fmt.iso(Date())) quarantined: \(reason)\n"
        try? note.write(to: Dirs.quarantine.appendingPathComponent(id + ".reason.txt"), atomically: true, encoding: .utf8)
        attempts[id] = nil; notBefore[id] = nil
        Self.setBad(id, 0); Self.setRehashed(id, false)
        ahaLog.error("quarantined \(id, privacy: .public): \(reason, privacy: .public)")
    }

    private static func badCount(_ id: String) -> Int {
        ((UserDefaults.standard.dictionary(forKey: badKey) as? [String: Int]) ?? [:])[id] ?? 0
    }
    private static func setBad(_ id: String, _ n: Int) {
        var d = (UserDefaults.standard.dictionary(forKey: badKey) as? [String: Int]) ?? [:]
        if n == 0 { d[id] = nil } else { d[id] = n }
        UserDefaults.standard.set(d, forKey: badKey)
    }
    private static func wasRehashed(_ id: String) -> Bool {
        (UserDefaults.standard.stringArray(forKey: rehashKey) ?? []).contains(id)
    }
    private static func setRehashed(_ id: String, _ on: Bool) {
        var a = Set(UserDefaults.standard.stringArray(forKey: rehashKey) ?? [])
        if on { a.insert(id) } else { a.remove(id) }
        UserDefaults.standard.set(Array(a), forKey: rehashKey)
    }

    /// 400/413/415/422: the server rejected this payload (typically sha256 mismatch). Retry up to 3 times, then
    /// recompute the sha once from the file on disk and retry another 3; then quarantine. Never retries forever.
    private func handleRejected(_ id: String, code: Int, audio: URL, side: URL) {
        let n = Self.badCount(id) + 1
        Self.setBad(id, n)
        if n < Self.maxBad { fail(id, "HTTP \(code) (\(n)/\(Self.maxBad))"); return }
        if !Self.wasRehashed(id) {
            Self.setRehashed(id, true)
            Self.setBad(id, 0)
            do {
                let sha = try Fmt.sha256Hex(of: audio)
                if let d = try? Data(contentsOf: side), var m = try? JSONDecoder().decode(SegmentMeta.self, from: d) {
                    let attrs = try? FileManager.default.attributesOfItem(atPath: audio.path)
                    let size = (attrs?[.size] as? Int) ?? m.bytes
                    let changed = sha != m.sha256
                    m.sha256 = sha; m.bytes = size
                    try JSONEncoder().encode(m).write(to: side, options: .atomic)
                    ahaLog.info("rehash \(id, privacy: .public) changed=\(changed)")
                }
                notBefore[id] = nil; attempts[id] = nil
                setError("\(id.prefix(15)): HTTP \(code) x3, sha recomputed, retrying")
            } catch {
                quarantineLocked(audio, reason: "HTTP \(code) x3 and rehash failed: \(error.localizedDescription)")
            }
        } else {
            quarantineLocked(audio, reason: "HTTP \(code) three times again after sha recompute")
            setError("\(id.prefix(15)): HTTP \(code), quarantined")
        }
    }

    /// Move an ACKed chunk to recordings/ (sidecar first, marked uploaded) so it stays playable until pruned.
    private func retain(audio: URL, side: URL) {
        let fm = FileManager.default
        let id = audio.deletingPathExtension().lastPathComponent
        let destAudio = Dirs.recordings.appendingPathComponent(audio.lastPathComponent)
        let destSide = Dirs.recordings.appendingPathComponent(id + ".json")
        guard let raw = try? Data(contentsOf: side), var m = try? JSONDecoder().decode(SegmentMeta.self, from: raw) else {
            try? fm.removeItem(at: audio); try? fm.removeItem(at: side); return
        }
        m.uploaded = true
        guard let d = try? JSONEncoder().encode(m), (try? d.write(to: destSide, options: .atomic)) != nil else {
            try? fm.removeItem(at: audio); try? fm.removeItem(at: side); return
        }
        try? fm.removeItem(at: destAudio)
        if (try? fm.moveItem(at: audio, to: destAudio)) == nil { try? fm.removeItem(at: audio); try? fm.removeItem(at: destSide) }
        try? fm.removeItem(at: side)
    }

    private func fail(_ id: String, _ msg: String) {
        let n = (attempts[id] ?? 0) + 1
        attempts[id] = n
        notBefore[id] = Date().addingTimeInterval(min(5 * pow(2, Double(n - 1)), 900))
        setError("\(id.prefix(15)): \(msg)")
        if priorityOutstanding.contains(id) { priorityFailedLocked(msg) }
    }

    // MARK: - URLSessionTaskDelegate (runs on session delegate queue; hop to state queue)

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let id = task.taskDescription else { return }
        let code = (task.response as? HTTPURLResponse)?.statusCode
        let errText = error?.localizedDescription
        q.async {
            self.inFlight.remove(id)
            let audio = Dirs.audio(in: Dirs.queue, id: id)
            let side = Dirs.queue.appendingPathComponent(id + ".json")
            if let errText { self.cachedBase = nil; self.fail(id, errText) }
            else if code == 200 || code == 201 {
                self.retain(audio: audio, side: side)               // ACK received: only now leave the queue (kept in recordings/)
                self.attempts[id] = nil; self.notBefore[id] = nil
                Self.setBad(id, 0); Self.setRehashed(id, false)
                self.priorityOutstanding.remove(id); self.checkPriorityDoneLocked()
                DispatchQueue.main.async { self.lastSuccess = Date(); self.lastError = nil }
            } else if code == 409 {
                self.quarantineLocked(audio, reason: "HTTP 409: id exists with different content")
                self.setError("\(id.prefix(15)): 409 conflict, quarantined")
            } else if let c = code, c == 400 || c == 413 || c == 415 || c == 422 {
                self.handleRejected(id, code: c, audio: audio, side: side)
            } else {
                self.fail(id, "HTTP \(code ?? -1)")
            }
            self.scanLocked()
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            self.bgCompletion?()
            self.bgCompletion = nil
        }
    }
}
