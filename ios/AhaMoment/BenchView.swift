import SwiftUI
import UIKit
import WhisperKit

// EXPERIMENT (hidden behind Settings > 实验 > 本地转写实测): on-device WhisperKit transcription of recordings
// still on the phone, compared on the Mac (asr/compare_ios.py) against the Mac's own transcript of the same audio.
// Nothing here runs unless the toggle is on and the user taps 「跑 10 条」.

extension Settings {
    static let benchLocalASRKey = "benchLocalASR"
    static let benchAttemptKey = "aha.bench.fullAttempt"     // set while loading the full turbo; still set at launch = it crashed (OOM)
}

private let fullVariant = "large-v3-v20240930_turbo"          // 1.64 GB, = Mac's whisper-large-v3-turbo
private let compressedVariant = "large-v3-v20240930_turbo_632MB"
private let benchCount = 10

private func footprintMB() -> Double {
    var info = task_vm_info_data_t()
    var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
    let kr = withUnsafeMutablePointer(to: &info) {
        $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count) }
    }
    return kr == KERN_SUCCESS ? Double(info.phys_footprint) / 1_048_576 : -1
}

private func thermalName(_ s: ProcessInfo.ThermalState) -> String {
    switch s { case .nominal: return "nominal"; case .fair: return "fair"; case .serious: return "serious"; case .critical: return "critical"; @unknown default: return "?" }
}

private func batteryName(_ s: UIDevice.BatteryState) -> String {
    switch s { case .unplugged: return "unplugged"; case .charging: return "charging"; case .full: return "full"; default: return "unknown" }
}

struct BenchCandidate: Identifiable {
    let chunk: RecChunk
    let macText: String
    var id: String { chunk.id }
    var seconds: Double { Double(chunk.meta.durationMs) / 1000 }
}

struct BenchRow: Identifiable {
    let id: String
    var state = "待跑"
    var transcribeS: Double?
}

@MainActor
final class BenchRunner: ObservableObject {
    @Published var candidates: [BenchCandidate] = []
    @Published var rows: [BenchRow] = []
    @Published var status = ""
    @Published var running = false
    @Published var variant = ""
    @Published var log: [String] = []

    private func say(_ s: String) { status = s; log.append(s); appendEvent("bench: \(s)") }

    private func bases() -> [URL] { Settings.baseURLs() }

    private func request(_ path: String, method: String = "GET", body: Data? = nil) async -> Data? {
        for base in bases() {
            var req = URLRequest(url: base.appendingPathComponent(path))
            req.httpMethod = method
            req.timeoutInterval = 8
            req.setValue("Bearer \(Settings.token)", forHTTPHeaderField: "Authorization")
            if let body { req.httpBody = body; req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
            if let (d, r) = try? await URLSession.shared.data(for: req), (r as? HTTPURLResponse)?.statusCode == 200 { return d }
        }
        return nil
    }

    func loadCandidates() async {
        say("正在找手机上还有、且 Mac 已有转写的录音…")
        let chunks = await Task.detached { RecordingStore.loadChunks().filter { $0.uploaded }.sorted { $0.recordedAt > $1.recordedAt } }.value
        var out: [BenchCandidate] = []
        for c in chunks.prefix(80) where out.count < benchCount {
            guard let d = await request("v1/items/\(c.id)"),
                  let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
                  let t = j["transcript"] as? String else { continue }
            if t.trimmingCharacters(in: .whitespacesAndNewlines).count >= 20 { out.append(BenchCandidate(chunk: c, macText: t)) }
        }
        candidates = out
        rows = out.map { BenchRow(id: $0.id) }
        say(out.isEmpty ? "没找到符合条件的录音（需要已上传、手机还留着、Mac 转写非空，且连上 Mac）" : "找到 \(out.count) 条")
    }

    private func appSupportModels() -> URL {
        let u = Dirs.root.appendingPathComponent("whisperkit", isDirectory: true)
        try? FileManager.default.createDirectory(at: u, withIntermediateDirectories: true)
        return u
    }

    private func load(_ name: String, downloadBase: URL) async throws -> (WhisperKit, Double, Double) {
        let t0 = Date()
        let folder = try await WhisperKit.download(variant: name, downloadBase: downloadBase) { p in
            Task { @MainActor in self.status = "下载 \(name) \(Int(p.fractionCompleted * 100))%" }
        }
        let dl = Date().timeIntervalSince(t0)
        let t1 = Date()
        let pipe = try await WhisperKit(WhisperKitConfig(modelFolder: folder.path, verbose: false, prewarm: true, load: true, download: false))
        return (pipe, dl, Date().timeIntervalSince(t1))
    }

    func run() async {
        guard !running else { return }
        running = true
        UIApplication.shared.isIdleTimerDisabled = true
        UIDevice.current.isBatteryMonitoringEnabled = true
        defer { running = false; UIApplication.shared.isIdleTimerDisabled = false }
        if candidates.isEmpty { await loadCandidates() }
        guard !candidates.isEmpty else { return }

        // 1. prompt (same text as the Mac)
        var prompt = ""
        if let d = await request("v1/asr/prompt"), let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            prompt = (j["prompt"] as? String) ?? ""
        }
        say("prompt \(prompt.count) 字；载入模型…")

        // 2. model: full turbo first, compressed fallback (also if the previous attempt died mid-load = OOM)
        let base = appSupportModels()
        var pipe: WhisperKit?
        var dlS = 0.0, loadS = 0.0
        var fallbackReason = ""
        if UserDefaults.standard.bool(forKey: Settings.benchAttemptKey) {
            fallbackReason = "上次载入完整 turbo 时 App 被杀（疑似 OOM）"
        } else {
            UserDefaults.standard.set(true, forKey: Settings.benchAttemptKey)
            do { let r = try await load(fullVariant, downloadBase: base); pipe = r.0; dlS = r.1; loadS = r.2; variant = fullVariant }
            catch { fallbackReason = "完整 turbo 载入失败：\(error.localizedDescription)" }
            UserDefaults.standard.set(false, forKey: Settings.benchAttemptKey)
        }
        if pipe == nil {
            say("\(fallbackReason)；改用压缩版")
            do { let r = try await load(compressedVariant, downloadBase: base); pipe = r.0; dlS = r.1; loadS = r.2; variant = compressedVariant }
            catch { say("压缩版也失败：\(error.localizedDescription)"); return }
        }
        guard let pipe else { return }
        say("模型 \(variant) 就绪：下载 \(String(format: "%.0f", dlS))s，载入 \(String(format: "%.0f", loadS))s")

        var opts = DecodingOptions(task: .transcribe, language: nil, usePrefillPrompt: true, detectLanguage: true)
        if !prompt.isEmpty, let tok = pipe.tokenizer {
            opts.promptTokens = tok.encode(text: " " + prompt.trimmingCharacters(in: .whitespaces))
                .filter { $0 < tok.specialTokens.specialTokenBegin }
        }

        // 3. sequential transcription
        for (i, c) in candidates.enumerated() {
            rows[i].state = "转写中"
            say("转写 \(i + 1)/\(candidates.count)")
            let availBefore = Double(os_proc_available_memory()) / 1_048_576
            var peak = footprintMB()
            let sampler = Task.detached {
                var m = 0.0
                while !Task.isCancelled { m = max(m, footprintMB()); try? await Task.sleep(nanoseconds: 200_000_000) }
                return m
            }
            let t0 = Date()
            var text = "", lang = "", err = ""
            do {
                let res = try await pipe.transcribe(audioPath: c.chunk.url.path, decodeOptions: opts)
                text = res.map { $0.text }.joined().trimmingCharacters(in: .whitespacesAndNewlines)
                lang = res.first?.language ?? ""
            } catch { err = error.localizedDescription }
            let secs = Date().timeIntervalSince(t0)
            sampler.cancel()
            peak = max(peak, await sampler.value)
            let rec: [String: Any] = [
                "id": c.id, "model": variant, "whisperkit": "1.1.0", "device": UIDevice.current.model,
                "audio_s": c.seconds, "download_s": dlS, "load_s": loadS, "transcribe_s": secs,
                "peak_footprint_mb": peak, "avail_mem_before_mb": availBefore,
                "avail_mem_after_mb": Double(os_proc_available_memory()) / 1_048_576,
                "battery_level": UIDevice.current.batteryLevel, "battery_state": batteryName(UIDevice.current.batteryState),
                "thermal_state": thermalName(ProcessInfo.processInfo.thermalState),
                "language": lang, "text": text, "error": err, "fallback_reason": fallbackReason,
                "ran_at": Fmt.iso(Date()),
            ]
            if let body = try? JSONSerialization.data(withJSONObject: rec) {
                let ok = await request("v1/bench/asr", method: "POST", body: body) != nil
                rows[i].state = (err.isEmpty ? "完成" : "出错") + (ok ? "" : "（未回传）")
            }
            rows[i].transcribeS = secs
        }
        say("全部完成，结果已发到 Mac")
    }
}

struct BenchView: View {
    @StateObject private var m = BenchRunner()

    var body: some View {
        List {
            Section {
                Text("在手机上用 WhisperKit（large-v3 turbo）转写最近 10 条还留在手机上的录音，结果发回 Mac 和 Mac 的转写对比。首次会下载约 1.6 GB 模型，请连 Wi-Fi/插电，转写期间保持 App 在前台（屏幕会保持常亮）。")
                    .font(.footnote).foregroundStyle(.secondary)
                Button(m.running ? "运行中…" : "跑 10 条") { Task { await m.run() } }.disabled(m.running)
                if !m.status.isEmpty { Text(m.status).font(.footnote) }
                if !m.variant.isEmpty { Text("模型：\(m.variant)").font(.footnote).foregroundStyle(.secondary) }
            }
            Section("录音（手机还在 + Mac 已转写）") {
                ForEach(Array(m.candidates.enumerated()), id: \.element.id) { i, c in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(c.id)  \(Int(c.seconds))s").font(.caption.monospaced())
                        Text(c.macText.prefix(40)).font(.caption).foregroundStyle(.secondary)
                        if i < m.rows.count {
                            Text(m.rows[i].state + (m.rows[i].transcribeS.map { String(format: "  %.1fs", $0) } ?? "")).font(.caption)
                        }
                    }
                }
            }
        }
        .navigationTitle("本地转写实测")
        .task { if m.candidates.isEmpty { await m.loadCandidates() } }
    }
}
