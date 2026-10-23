// Aha Moment for Mac: a menu-bar microphone. Click (or tap Ctrl+Shift) to start, again to stop.
// Same contract as the iPhone app: 60 s audio chunks sharing one session_id are PUT to the local
// server (/v1/audio/{id}); the server transcribes and rebuilds contexts/daily_records/*_aha.md.
// Chunks wait in ~/.aha/mac_outbox until the server ACKs (2xx), so nothing is lost if it is down.
import AppKit
import AVFoundation
import ApplicationServices
import CryptoKit

let chunkSeconds = 60.0
let server = URL(string: ProcessInfo.processInfo.environment["AHA_SERVER"] ?? "http://127.0.0.1:8765")!
let outbox = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".aha/mac_outbox")

func iso(_ d: Date) -> String {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = .current
    return f.string(from: d)
}

func log(_ s: String) {
    let line = "\(iso(Date())) \(s)\n"
    let url = outbox.appendingPathComponent("events.log")
    if let h = try? FileHandle(forWritingTo: url) {
        h.seekToEndOfFile(); h.write(line.data(using: .utf8)!); try? h.close()
    } else {
        try? line.write(to: url, atomically: true, encoding: .utf8)
    }
}

// MARK: - Recorder: mic -> rotating m4a chunks (no gap: rotation happens between tap buffers)

final class Recorder {
    private let engine = AVAudioEngine()
    private var file: AVAudioFile?
    private var chunkURL: URL?
    private var chunkStart = Date()
    private var sessionId = ""
    private var sessionStart = Date()
    private var index = 0
    private let q = DispatchQueue(label: "aha.recorder")
    var onChunk: ((URL) -> Void)?

    var isRecording: Bool { engine.isRunning }

    func start() throws {
        sessionStart = Date()
        let df = DateFormatter(); df.dateFormat = "yyyyMMdd'T'HHmmss"
        sessionId = "mac" + df.string(from: sessionStart) + "-" + String(UInt32.random(in: 0...UInt32.max), radix: 16)
        index = 0
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0 else { throw NSError(domain: "aha", code: 1, userInfo: [NSLocalizedDescriptionKey: "没有可用的麦克风"]) }
        try q.sync { try openChunk(fmt, at: Date()) }
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { [weak self] buf, _ in
            guard let self else { return }
            self.q.async {
                if Date().timeIntervalSince(self.chunkStart) >= chunkSeconds {
                    self.closeChunk()
                    try? self.openChunk(fmt, at: Date())
                }
                try? self.file?.write(from: buf)
            }
        }
        engine.prepare()
        try engine.start()
        log("start \(sessionId)")
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        q.sync { closeChunk() }
        log("stop \(sessionId)")
    }

    private func openChunk(_ fmt: AVAudioFormat, at t: Date) throws {
        chunkStart = t
        let id = "\(sessionId)-\(String(format: "%03d", index))"
        index += 1
        let url = outbox.appendingPathComponent(id + ".m4a")
        let settings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: fmt.sampleRate,
                                       AVNumberOfChannelsKey: min(fmt.channelCount, 2), AVEncoderBitRateKey: 64000]
        file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: fmt.commonFormat,
                               interleaved: fmt.isInterleaved)
        chunkURL = url
        let meta: [String: Any] = ["id": id, "recorded_at": iso(t), "session_id": sessionId,
                                   "session_start": iso(sessionStart)]
        try JSONSerialization.data(withJSONObject: meta).write(to: url.deletingPathExtension().appendingPathExtension("json"))
    }

    private func closeChunk() {
        guard let url = chunkURL else { return }
        let ms = Int(Date().timeIntervalSince(chunkStart) * 1000)
        file = nil   // releasing AVAudioFile finalizes the m4a
        chunkURL = nil
        let jsonURL = url.deletingPathExtension().appendingPathExtension("json")
        if var meta = (try? JSONSerialization.jsonObject(with: Data(contentsOf: jsonURL))) as? [String: Any] {
            meta["duration_ms"] = ms
            meta["ready"] = true
            try? JSONSerialization.data(withJSONObject: meta).write(to: jsonURL)
        }
        onChunk?(url)
    }
}

// MARK: - Uploader: outbox -> PUT /v1/audio/{id}; delete only after 2xx

final class Uploader {
    private let q = DispatchQueue(label: "aha.uploader")
    private var busy = false
    var onState: ((Int) -> Void)?   // pending count

    init() {
        Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in self?.kick() }
    }

    func pending() -> [URL] {
        let all = (try? FileManager.default.contentsOfDirectory(at: outbox, includingPropertiesForKeys: nil)) ?? []
        return all.filter { $0.pathExtension == "json" && meta($0)?["ready"] as? Bool == true }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    private func meta(_ u: URL) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: Data(contentsOf: u))) as? [String: Any]
    }

    func kick() {
        q.async {
            guard !self.busy else { return }
            self.busy = true
            defer { self.busy = false }
            for j in self.pending() {
                if !self.upload(j) { break }
            }
            let n = self.pending().count
            DispatchQueue.main.async { self.onState?(n) }
        }
    }

    private func upload(_ jsonURL: URL) -> Bool {
        guard let m = meta(jsonURL), let id = m["id"] as? String else { return true }
        let audio = jsonURL.deletingPathExtension().appendingPathExtension("m4a")
        guard let data = try? Data(contentsOf: audio), !data.isEmpty else {
            try? FileManager.default.removeItem(at: jsonURL); try? FileManager.default.removeItem(at: audio)
            log("drop empty \(id)"); return true
        }
        let tokenFile = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".aha/token")
        guard let token = try? String(contentsOf: tokenFile, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines) else {
            log("no token at ~/.aha/token"); return false
        }
        var req = URLRequest(url: server.appendingPathComponent("v1/audio/\(id)"))
        req.httpMethod = "PUT"
        req.timeoutInterval = 60
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        req.setValue("audio/mp4", forHTTPHeaderField: "Content-Type")
        req.setValue(SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(), forHTTPHeaderField: "X-Aha-Sha256")
        req.setValue(m["recorded_at"] as? String, forHTTPHeaderField: "X-Aha-Recorded-At")
        req.setValue("ambient", forHTTPHeaderField: "X-Aha-Source")
        req.setValue("high", forHTTPHeaderField: "X-Aha-Priority")
        req.setValue(m["session_id"] as? String, forHTTPHeaderField: "X-Aha-Session")
        req.setValue(m["session_start"] as? String, forHTTPHeaderField: "X-Aha-Session-Start")
        if let ms = m["duration_ms"] as? Int { req.setValue(String(ms), forHTTPHeaderField: "X-Aha-Duration-Ms") }
        let sem = DispatchSemaphore(value: 0)
        var code = 0
        URLSession.shared.uploadTask(with: req, from: data) { _, resp, err in
            code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            if let err { log("upload \(id) error: \(err.localizedDescription)") }
            sem.signal()
        }.resume()
        sem.wait()
        if (200..<300).contains(code) {
            try? FileManager.default.removeItem(at: audio)
            try? FileManager.default.removeItem(at: jsonURL)
            log("uploaded \(id) (\(code))")
            return true
        }
        log("upload \(id) failed: HTTP \(code)")
        return false
    }
}

// MARK: - Menu bar

final class App: NSObject, NSApplicationDelegate {
    let recorder = Recorder()
    let uploader = Uploader()
    var item: NSStatusItem!
    var pendingCount = 0
    var chordDownAt: Date?
    var chordCancelled = false
    var monitors: [Any] = []

    func applicationDidFinishLaunching(_ n: Notification) {
        try? FileManager.default.createDirectory(at: outbox, withIntermediateDirectories: true)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.target = self
        item.button?.action = #selector(clicked)
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        recorder.onChunk = { [weak self] _ in self?.uploader.kick() }
        uploader.onState = { [weak self] n in self?.pendingCount = n; self?.refresh() }
        setupChordHotkey()
        refresh()
        uploader.kick()
    }

    func refresh() {
        let rec = recorder.isRecording
        let img = NSImage(systemSymbolName: rec ? "record.circle.fill" : "mic", accessibilityDescription: "Aha")
        if rec {
            item.button?.image = img?.withSymbolConfiguration(.init(paletteColors: [.systemRed]))
        } else {
            img?.isTemplate = true
            item.button?.image = img
        }
        item.button?.toolTip = rec ? "录音中，点一下停止 (或单按 Ctrl+Shift)" :
            (pendingCount > 0 ? "\(pendingCount) 段待上传" : "点一下开始录音 (或单按 Ctrl+Shift)")
    }

    @objc func clicked() {
        if NSApp.currentEvent?.type == .rightMouseUp { showMenu(); return }
        toggle()
    }

    func toggle() {
        if recorder.isRecording {
            recorder.stop()
        } else {
            AVCaptureDevice.requestAccess(for: .audio) { ok in
                DispatchQueue.main.async {
                    guard ok else { self.alert("没有麦克风权限：系统设置 → 隐私与安全性 → 麦克风，打开 AhaMac"); return }
                    do { try self.recorder.start() } catch { self.alert("录音启动失败：\(error.localizedDescription)") }
                    self.refresh()
                }
            }
        }
        refresh()
    }

    func showMenu() {
        let menu = NSMenu()
        if pendingCount > 0 { menu.addItem(withTitle: "\(pendingCount) 段待上传", action: nil, keyEquivalent: "") }
        menu.addItem(withTitle: "打开 daily_records", action: #selector(openRecords), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "退出", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
        item.button?.performClick(nil)
        item.menu = nil
    }

    @objc func openRecords() {
        let ws = ProcessInfo.processInfo.environment["AHA_CONTEXT_ROOT"] ?? NSString("~/work/context-infrastructure").expandingTildeInPath
        NSWorkspace.shared.open(URL(fileURLWithPath: ws).appendingPathComponent("contexts/daily_records"))
    }

    @objc func quit() {
        if recorder.isRecording { recorder.stop() }
        uploader.kick()
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { NSApp.terminate(nil) }
    }

    func alert(_ s: String) {
        let a = NSAlert(); a.messageText = s; a.runModal()
    }

    // Ctrl+Shift tap (modifier-only chord). Fires when both are down then released within 0.6 s with
    // no other key / Cmd / Option in between. Needs Accessibility for the global monitor; the
    // menu-bar click works regardless.
    func setupChordHotkey() {
        let trusted = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
        log("accessibility trusted=\(trusted) (Ctrl+Shift hotkey \(trusted ? "active" : "inactive until granted"))")
        let mask: NSEvent.EventTypeMask = [.flagsChanged, .keyDown]
        if let m = NSEvent.addGlobalMonitorForEvents(matching: mask, handler: { [weak self] e in self?.chordEvent(e) }) { monitors.append(m) }
        if let m = NSEvent.addLocalMonitorForEvents(matching: mask, handler: { [weak self] e in self?.chordEvent(e); return e }) { monitors.append(m) }
    }

    func chordEvent(_ e: NSEvent) {
        if e.type == .keyDown { chordCancelled = true; chordDownAt = nil; return }
        let f = e.modifierFlags.intersection([.control, .shift, .command, .option])
        if f.contains(.command) || f.contains(.option) { chordCancelled = true; chordDownAt = nil; return }
        if f == [.control, .shift] {
            if chordDownAt == nil && !chordCancelled { chordDownAt = Date() }
        } else {
            if let t = chordDownAt, !chordCancelled, Date().timeIntervalSince(t) < 0.6 {
                DispatchQueue.main.async { self.toggle() }
            }
            chordDownAt = nil
            if f.isEmpty { chordCancelled = false }
        }
    }
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
