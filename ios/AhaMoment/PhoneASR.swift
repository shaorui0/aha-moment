import AVFoundation
import Network
import UIKit
import WhisperKit

// On-device ASR as an instant PREVIEW for the Review screen. The Mac (whisper-large-v3-turbo + Ollama enhance) stays the
// authoritative transcript; nothing here changes the audio format or the upload. Each closed chunk gets
// Application Support/AhaMoment/phone/<chunk id>.txt (outside queue/ and recordings/, which move files after upload).

extension Settings {
    static let useOnDeviceAsrKey = "useOnDeviceAsr"      // default ON
    static let phoneAsrModelKey = "phoneAsrModel"        // tiny | base | small (WhisperKit variant openai_whisper-<name>)
    static var useOnDeviceAsr: Bool { UserDefaults.standard.object(forKey: useOnDeviceAsrKey) as? Bool ?? true }
    static var phoneAsrModel: String {
        let v = UserDefaults.standard.string(forKey: phoneAsrModelKey) ?? ""
        return PhoneASR.models.contains { $0.0 == v } ? v : "base"
    }
}

enum PhoneText {
    static func url(_ id: String) -> URL { Dirs.phone.appendingPathComponent(id + ".txt") }
    static func exists(_ id: String) -> Bool { FileManager.default.fileExists(atPath: url(id).path) }
    static func read(_ id: String) -> String? { try? String(contentsOf: url(id), encoding: .utf8) }
    /// Same join as the Mac (one line per chunk, in recording order); chunks without text are left out.
    static func combined(_ chunks: [RecChunk]) -> String {
        chunks.compactMap { read($0.meta.id)?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }.joined(separator: "\n")
    }
}

/// One line for the UI: "下载模型 base 42%" / "载入模型 base…" / "已就绪（base）" / "不可用：<why>".
@MainActor
final class PhoneASRStatus: ObservableObject {
    static let shared = PhoneASRStatus()
    @Published var line = ""
}

enum PhoneASRError: LocalizedError {
    case notDownloaded(String)
    var errorDescription: String? {
        switch self { case .notDownloaded(let m): return "模型 \(m) 还没下载（Wi-Fi 下会自动下载，或在设置里手动开启）" }
    }
}

/// Owns the single WhisperKit pipeline (loaded lazily, kept for the app session) and runs jobs strictly one after another.
actor PhoneASR {
    static let shared = PhoneASR()
    /// (id, label): sizes are the CoreML folder sizes on argmaxinc/whisperkit-coreml.
    static let models = [("tiny", "tiny · 77 MB · 最快"), ("base", "base · 147 MB · 默认"), ("small", "small · 486 MB · 更准")]

    private var pipe: WhisperKit?
    private var pipeModel = ""
    private var tail: Task<Void, Never>?
    private var queued = Set<String>()
    private var lastLoggedError = ""

    // MARK: - entry points (any thread, fire and forget)

    /// Segment closed (SegmentWriter.onFinished). Only while the app is in the foreground: a model load / specialization
    /// must never compete with the background recording; chunks skipped here are picked up when Review opens.
    nonisolated func chunkFinalized(_ meta: SegmentMeta) {
        guard Settings.useOnDeviceAsr, Self.shouldTranscribe(meta) else { return }
        Task.detached(priority: .userInitiated) {
            let fg = await MainActor.run { UIApplication.shared.applicationState == .active }
            if fg { await self.submit(meta) }
        }
    }

    /// Review opened: transcribe whichever of these chunks has no phone text yet.
    nonisolated func ensure(_ chunks: [RecChunk]) {
        guard Settings.useOnDeviceAsr else { return }
        let todo = chunks.map(\.meta).filter { Self.shouldTranscribe($0) && !PhoneText.exists($0.id) }
        guard !todo.isEmpty else { return }
        Task.detached(priority: .userInitiated) { for m in todo { await self.submit(m) } }
    }

    /// Load the model now (record start: ready by the time the user stops). `userInitiated` (toggle / model picker) may
    /// download on any network; otherwise only when it is already on disk or the network is Wi-Fi-like.
    nonisolated func warmUp(userInitiated: Bool) {
        guard Settings.useOnDeviceAsr else { return }
        Task.detached(priority: .utility) { await self.enqueueWarm(userInitiated) }
    }

    /// < 1 s or silent (peak per-buffer RMS under the same threshold as "skip silent segments"). peakRms 0 = unknown (recovered orphan).
    static func shouldTranscribe(_ m: SegmentMeta) -> Bool {
        m.durationMs >= 1000 && !(m.peakRms > 0 && m.peakRms < Settings.silenceRMS)
    }

    // MARK: - serial job chain

    private func chain(_ job: @escaping @Sendable () async -> Void) {
        let prev = tail
        tail = Task { await prev?.value; await job() }
    }

    private func submit(_ meta: SegmentMeta) {
        guard !queued.contains(meta.id), !PhoneText.exists(meta.id) else { return }
        queued.insert(meta.id)
        chain { await self.transcribe(meta) }
    }

    private func enqueueWarm(_ userInitiated: Bool) {
        chain { await self.warm(userInitiated) }
    }

    private func warm(_ userInitiated: Bool) async {
        do { _ = try await pipeline(allowDownload: userInitiated ? true : await Self.cheapNetwork()) }
        catch { noteFailure("warm-up", error) }
    }

    private func transcribe(_ meta: SegmentMeta) async {
        defer { queued.remove(meta.id) }
        guard !PhoneText.exists(meta.id), Settings.useOnDeviceAsr, let audio = Self.audioURL(meta.id) else { return }
        do {
            let p = try await pipeline(allowDownload: await Self.cheapNetwork())
            let t0 = Date()
            let opts = DecodingOptions(task: .transcribe, language: nil, usePrefillPrompt: true, detectLanguage: true,
                                       skipSpecialTokens: true, withoutTimestamps: true)
            let res = try await p.transcribe(audioPath: audio.path, decodeOptions: opts)
            let text = Self.clean(res.map { $0.text }.joined(separator: " "))
            try text.write(to: PhoneText.url(meta.id), atomically: true, encoding: .utf8)    // empty text is written too: do not retry
            appendEvent("phone-asr: \(meta.id) model=\(pipeModel) audio=\(meta.durationMs)ms took=\(String(format: "%.1f", Date().timeIntervalSince(t0)))s chars=\(text.count)")
        } catch { noteFailure(meta.id, error) }
    }

    private func noteFailure(_ what: String, _ error: Error) {
        let m = error.localizedDescription
        Self.say("不可用：\(m)")
        if m != lastLoggedError { lastLoggedError = m; appendEvent("phone-asr: \(what) failed: \(m)") }     // not every 5 s from the Review refresh
    }

    // MARK: - model

    private func pipeline(allowDownload: Bool) async throws -> WhisperKit {
        let model = Settings.phoneAsrModel
        if let p = pipe, pipeModel == model { return p }
        pipe = nil
        let variant = "openai_whisper-\(model)"
        var folder = Self.localFolder(variant)
        if !FileManager.default.fileExists(atPath: folder.appendingPathComponent("AudioEncoder.mlmodelc").path) {
            guard allowDownload else { throw PhoneASRError.notDownloaded(model) }
            Self.say("下载模型 \(model) 0%")
            var last = -1
            folder = try await WhisperKit.download(variant: variant, downloadBase: Self.modelsBase) { pr in
                let pct = Int(pr.fractionCompleted * 100)
                if pct != last { last = pct; Self.say("下载模型 \(model) \(pct)%") }
            }
        }
        Self.say("载入模型 \(model)…")
        let t0 = Date()
        // Mel on CPU: no GPU work, so a transcription that is still running when the app goes to the background cannot fail on it.
        let cfg = WhisperKitConfig(modelFolder: folder.path, computeOptions: ModelComputeOptions(melCompute: .cpuOnly),
                                   verbose: false, prewarm: true, load: true, download: false)
        let p = try await WhisperKit(cfg)
        pipe = p; pipeModel = model; lastLoggedError = ""
        Self.say("已就绪（\(model)）")
        appendEvent("phone-asr: model \(model) ready in \(String(format: "%.1f", Date().timeIntervalSince(t0)))s")
        return p
    }

    /// Same download root as BenchView, so a model fetched by either is reused.
    static var modelsBase: URL {
        let u = Dirs.root.appendingPathComponent("whisperkit", isDirectory: true)
        try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
        return u
    }
    static func localFolder(_ variant: String) -> URL {
        modelsBase.appendingPathComponent("models/argmaxinc/whisperkit-coreml/\(variant)", isDirectory: true)
    }

    // MARK: - helpers

    /// The chunk may have moved from queue/ to recordings/ after the upload ACK.
    private static func audioURL(_ id: String) -> URL? {
        for dir in [Dirs.queue, Dirs.recordings] {
            let u = Dirs.audio(in: dir, id: id)
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        return nil
    }

    private static func clean(_ s: String) -> String {
        s.replacingOccurrences(of: "<\\|[^|]*\\|>", with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// True when a model download may start without asking: connected and not cellular / hotspot / Low Data Mode.
    private static func cheapNetwork() async -> Bool {
        await withCheckedContinuation { (cont: CheckedContinuation<Bool, Never>) in
            let m = NWPathMonitor()
            var done = false
            m.pathUpdateHandler = { p in
                guard !done else { return }
                done = true; m.cancel()
                cont.resume(returning: p.status == .satisfied && !p.isExpensive && !p.isConstrained)
            }
            m.start(queue: DispatchQueue(label: "aha.phoneasr.net"))
        }
    }

    private static func say(_ s: String) { Task { @MainActor in PhoneASRStatus.shared.line = s } }
}
