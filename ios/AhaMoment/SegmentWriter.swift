import AVFoundation
import Accelerate
import CryptoKit

/// Sidecar JSON stored next to `<id>.aac` (older builds: `.m4a`). Its existence means "audio is closed and ready to upload".
struct SegmentMeta: Codable {
    var id: String
    var recordedAt: String      // ISO8601 with timezone
    var durationMs: Int
    var source: String          // always "ambient" now (server accepts it); old sidecars may say "quick"
    var sha256: String
    var peakRms: Float
    var bytes: Int
    // Recording-session fields (all optional: sidecars written by older builds still decode; server never sees the sidecar).
    var sessionId: String?          // the recording (start->stop run) this chunk belongs to; nil for legacy chunks
    var sessionKind: String?        // legacy, no longer written
    var sessionStart: String?       // ISO8601 start of the recording
    var ambientSessionId: String?   // legacy, no longer written
    var ambientSessionStart: String?
    var uploaded: Bool?             // true once ACKed (file then lives in recordings/)
    var priority: Bool?             // true = upload first, even in record-only mode, with X-Aha-Priority: high
    var tags: [String]?             // sent as X-Aha-Tags (comma-separated); persisted so retries keep them

    enum CodingKeys: String, CodingKey {
        case id, source, sha256, bytes, uploaded, priority, tags
        case sessionId = "session_id"
        case sessionKind = "session_kind"
        case sessionStart = "session_start"
        case ambientSessionId = "ambient_session_id"
        case ambientSessionStart = "ambient_session_start"
        case recordedAt = "recorded_at"
        case durationMs = "duration_ms"
        case peakRms = "peak_rms"
    }
}

/// One "recording" as the user sees it: one start->stop run.
struct SessionRef: Equatable {
    let id: String
    let start: Date
}

enum Dirs {
    static let root: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("AhaMoment", isDirectory: true)
    }()
    static let queue = root.appendingPathComponent("queue", isDirectory: true)
    static let quarantine = root.appendingPathComponent("quarantine", isDirectory: true)
    static let recordings = root.appendingPathComponent("recordings", isDirectory: true)   // uploaded chunks kept for playback
    static let phone = root.appendingPathComponent("phone", isDirectory: true)             // on-device transcript preview <id>.txt (PhoneASR)

    /// New chunks are ADTS AAC (`.aac`): every frame is self-framed, so a file cut off by a kill/crash stays readable up
    /// to the last flushed frame. `.m4a` (moov atom written at close) and `.caf` (packet table at close) lose the whole
    /// file. `.m4a` is still read for chunks recorded by older builds.
    static let audioExt = "aac"
    static let audioExts: Set<String> = ["aac", "m4a"]
    static func audio(in dir: URL, id: String) -> URL {
        let fm = FileManager.default
        for e in [audioExt, "m4a"] {
            let u = dir.appendingPathComponent("\(id).\(e)")
            if fm.fileExists(atPath: u.path) { return u }
        }
        return dir.appendingPathComponent("\(id).\(audioExt)")
    }
    static func contentType(_ audio: URL) -> String { audio.pathExtension == "aac" ? "audio/aac" : "audio/m4a" }

    static func prepare() {
        for var d in [queue, quarantine, recordings, phone] {
            try? FileManager.default.createDirectory(
                at: d, withIntermediateDirectories: true,
                attributes: [.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication])
            var v = URLResourceValues(); v.isExcludedFromBackup = true
            try? d.setResourceValues(v)
        }
    }
}

enum Fmt {
    static func iso(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = .current
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: d)
    }
    static func parse(_ s: String?) -> Date? {
        guard let s else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }
    static func newSegmentID(_ d: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd'T'HHmmss"
        return f.string(from: d) + "_" + String(UUID().uuidString.prefix(8)).lowercased()
    }
    static func sha256Hex(of url: URL) throws -> String {
        let h = try FileHandle(forReadingFrom: url)
        defer { try? h.close() }
        var hasher = SHA256()
        while let chunk = try h.read(upToCount: 1 << 20), !chunk.isEmpty { hasher.update(data: chunk) }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}

/// Converts input-tap buffers to AAC 16kHz mono and writes them into rotating ADTS `.aac` segments.
/// `append` is called from the audio tap thread; closing/hashing is pushed to a utility queue.
final class SegmentWriter: @unchecked Sendable {
    static let pcm = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16000, channels: 1, interleaved: false)!

    private final class Seg {
        var file: AVAudioFile?
        let id: String, url: URL, recordedAt: Date
        var session: SessionRef?     // snapshot at open time
        var frames: Int64 = 0
        var peak: Float = 0
        var priority = false
        init(file: AVAudioFile, id: String, url: URL, recordedAt: Date) {
            self.file = file; self.id = id; self.url = url; self.recordedAt = recordedAt
        }
    }

    private let lock = NSLock()
    private let finalizeQ = DispatchQueue(label: "aha.finalize", qos: .utility)
    private let converter: AVAudioConverter
    private let ambientSeconds: Int
    private var sessionRef: SessionRef?
    private var cur: Seg?
    private var outBuf: AVAudioPCMBuffer?      // reused conversion output (append runs serially on the tap thread)
    private(set) var lastError: String?
    /// Called on a background queue once a segment is closed and its sidecar is written.
    var onFinished: (@Sendable (SegmentMeta) -> Void)?
    /// Called (any thread) once per distinct write/open/convert/finalize error, not per buffer.
    var onError: (@Sendable (String) -> Void)?

    // Heartbeat: time of the last buffer received from the tap. Written on the tap thread, read from the main-queue timer.
    private let hbLock = NSLock()
    private var _lastAppendAt = CFAbsoluteTimeGetCurrent()
    var lastAppendAt: CFAbsoluteTime { hbLock.lock(); defer { hbLock.unlock() }; return _lastAppendAt }
    private func touch() { hbLock.lock(); _lastAppendAt = CFAbsoluteTimeGetCurrent(); hbLock.unlock() }

    // Level meter: last N per-buffer RMS values (~6/s), reusing the RMS already computed for silence detection.
    // Only read by the foreground waveform view; costs one array write per buffer.
    static let levelCount = 48
    private var _levels = [Float](repeating: 0, count: SegmentWriter.levelCount)
    private var levelPos = 0
    /// Oldest -> newest.
    var levels: [Float] {
        hbLock.lock(); defer { hbLock.unlock() }
        return Array(_levels[levelPos...] + _levels[..<levelPos])
    }
    private func pushLevel(_ v: Float) {
        hbLock.lock(); _levels[levelPos] = v; levelPos = (levelPos + 1) % Self.levelCount; hbLock.unlock()
    }

    private var lastReported: String?      // dedupe for onError; cleared by a successful write
    private var nextOpenAt: CFAbsoluteTime = 0   // cooldown after a failed open/write so a full disk does not spin
    private func report(_ m: String) {
        lastError = m
        hbLock.lock(); let fresh = lastReported != m; lastReported = m; hbLock.unlock()
        if fresh { onError?(m) }
    }
    private func clearReported() {
        hbLock.lock(); lastReported = nil; hbLock.unlock()
    }

    init?(inputFormat: AVAudioFormat, segmentSeconds: Int, session: SessionRef?) {
        guard let c = AVAudioConverter(from: inputFormat, to: Self.pcm) else { return nil }
        converter = c
        sessionRef = session
        ambientSeconds = segmentSeconds
    }

    private var limitFrames: Int64 { Int64(ambientSeconds) * 16000 }

    func append(_ buf: AVAudioPCMBuffer) {
        touch()
        let cap = AVAudioFrameCount(Double(buf.frameLength) * 16000 / buf.format.sampleRate) + 64
        if outBuf == nil || outBuf!.frameCapacity < cap { outBuf = AVAudioPCMBuffer(pcmFormat: Self.pcm, frameCapacity: cap) }
        guard let out = outBuf else { return }
        out.frameLength = 0
        var fed = false
        var err: NSError?
        let st = converter.convert(to: out, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true; status.pointee = .haveData; return buf
        }
        guard st != .error, out.frameLength > 0, let ch = out.floatChannelData?[0] else {
            if let err { report("convert: \(err.localizedDescription)") }
            return
        }
        var ms: Float = 0
        vDSP_measqv(ch, 1, &ms, vDSP_Length(out.frameLength))   // mean of squares
        let rms = ms.squareRoot()
        pushLevel(rms)

        lock.lock(); defer { lock.unlock() }
        if cur == nil {
            if CFAbsoluteTimeGetCurrent() < nextOpenAt { return }   // cooling down after a failure: drop, do not spin
            openLocked()
        }
        guard let s = cur, let f = s.file else { return }
        do { try f.write(from: out); clearReported() }
        catch { report("write: \(error.localizedDescription)"); nextOpenAt = CFAbsoluteTimeGetCurrent() + 5; closeLocked(); return }
        s.frames += Int64(out.frameLength)
        s.peak = max(s.peak, rms)
        if s.frames >= limitFrames { closeLocked() }   // next buffer opens a new file: gapless at buffer granularity
    }

    /// Close current segment now (stop, interruption, route change).
    func finish() { lock.lock(); closeLocked(); lock.unlock() }

    /// Priority send: close the current segment now, flagged priority; the next buffer opens a new one (same session ids).
    func rotatePriority() { lock.lock(); cur?.priority = true; closeLocked(); lock.unlock() }

    /// New recording started while the engine is already running: close the current chunk, tag following chunks with it.
    func setSession(_ r: SessionRef?) {
        lock.lock(); closeLocked(); sessionRef = r; lock.unlock()
    }

    // MARK: - private (lock held)

    private func openLocked() {
        let now = Date()
        let id = Fmt.newSegmentID(now)
        let url = Dirs.queue.appendingPathComponent(id + "." + Dirs.audioExt)
        let settings: [String: Any] = [AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                                       AVSampleRateKey: 16000,
                                       AVNumberOfChannelsKey: 1,
                                       AVEncoderBitRateKey: 32000]
        do {
            let f = try AVAudioFile(forWriting: url, settings: settings,
                                    commonFormat: .pcmFormatFloat32, interleaved: false)
            let seg = Seg(file: f, id: id, url: url, recordedAt: now)
            seg.session = sessionRef
            cur = seg
        } catch { report("open: \(error.localizedDescription)"); nextOpenAt = CFAbsoluteTimeGetCurrent() + 5 }
    }

    private func closeLocked() {
        guard let s = cur else { return }
        cur = nil
        // Strong capture on purpose: teardown() drops the writer right after finish(); a weak self would be nil here
        // and the sidecar would never be written (segment stuck in queue until next launch's orphan recovery).
        finalizeQ.async {
            s.file = nil   // releases AVAudioFile -> flushes and closes the m4a
            self.finalize(s)
        }
    }

    private func finalize(_ s: Seg) {
        let ms = Int(s.frames * 1000 / 16000)
        let fm = FileManager.default
        if ms < 300 { try? fm.removeItem(at: s.url); return }   // accidental tap
        if Settings.skipSilence && s.peak < Settings.silenceRMS {
            try? fm.removeItem(at: s.url); return
        }
        do {
            let size = (try fm.attributesOfItem(atPath: s.url.path)[.size] as? Int) ?? 0
            let meta = SegmentMeta(id: s.id, recordedAt: Fmt.iso(s.recordedAt), durationMs: ms, source: "ambient",
                                   sha256: try Fmt.sha256Hex(of: s.url), peakRms: s.peak, bytes: size,
                                   sessionId: s.session?.id,
                                   sessionStart: s.session.map { Fmt.iso($0.start) },
                                   priority: s.priority ? true : nil,
                                   tags: s.session.map { Settings.claudeSessions.contains($0.id) } == true ? ["claude"] : nil)
            try JSONEncoder().encode(meta).write(to: Dirs.queue.appendingPathComponent(s.id + ".json"), options: .atomic)
            onFinished?(meta)
        } catch { report("finalize: \(error.localizedDescription)") }
    }

    /// Crash recovery: an audio file without sidecar was never closed. Try to salvage it.
    static func recoverOrphan(audio: URL) -> SegmentMeta? {
        let id = audio.deletingPathExtension().lastPathComponent
        guard let f = try? AVAudioFile(forReading: audio), f.length > 0,
              let sha = try? Fmt.sha256Hex(of: audio) else { return nil }
        let created = (try? audio.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? Date()
        let ms = Int(Double(f.length) / f.processingFormat.sampleRate * 1000)
        let size = ((try? FileManager.default.attributesOfItem(atPath: audio.path))?[.size] as? Int) ?? 0
        // Belongs to the persisted recording if it started before this file; otherwise its own one-chunk recording.
        let amb = Settings.ambientSession
        let join = amb.map { Settings.captureDesired && $0.start <= created } ?? false
        let ref = join ? amb! : SessionRef(id: id, start: created)
        return SegmentMeta(id: id, recordedAt: Fmt.iso(created), durationMs: ms, source: "ambient",
                           sha256: sha, peakRms: 0, bytes: size,
                           sessionId: ref.id, sessionStart: Fmt.iso(ref.start))
    }
}
