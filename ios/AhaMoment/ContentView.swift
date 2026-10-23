import SwiftUI

struct ContentView: View {
    @EnvironmentObject var capture: CaptureEngine
    @EnvironmentObject var queue: UploadQueue
    @Environment(\.scenePhase) private var phase
    @State private var showSettings = false
    @State private var showQueue = false
    @State private var showRecordings = false
    @AppStorage(Settings.uploadModeKey) private var mode = UploadMode.auto.rawValue
    @AppStorage(Settings.useOnDeviceAsrKey) private var useOnDevice = true
    @ObservedObject private var asr = PhoneASRStatus.shared

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                recordButton
                Button { showRecordings = true } label: {
                    Label("Recordings", systemImage: "waveform").frame(maxWidth: .infinity)
                }.buttonStyle(.bordered)
                autoUploadButton
                phoneAsrToggle
                stats
                Spacer()
            }
            .padding()
            .navigationTitle("Aha Moment")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Queue") { showQueue = true } }
                ToolbarItem(placement: .topBarTrailing) { Button("Settings") { showSettings = true } }
            }
            .sheet(isPresented: $showSettings) { SettingsView() }
            .sheet(isPresented: $showQueue) { QueueView() }
            .sheet(isPresented: $showRecordings) { RecordingsView() }
            .task(id: useOnDevice) {
                // Kick off model download the first time the app opens with the toggle ON — otherwise the default-true
                // setting never fires .onChange and no chunk ever gets a phone transcript. Safe to call repeatedly:
                // PhoneASR.warmUp is idempotent and only downloads if network is cheap (or user-initiated).
                if useOnDevice { PhoneASR.shared.warmUp(userInitiated: false) }
            }
        }
    }

    private var recordButton: some View {
        Button { capture.setRecording(!capture.isOn) } label: {
            VStack(spacing: 8) {
                Image(systemName: capture.isOn ? "stop.circle.fill" : "mic.circle.fill").font(.system(size: 64))
                Text(capture.isOn ? "录音中 · 点按停止" : "点按开始录音").font(.title2.bold())
                if capture.isOn, phase == .active {
                    let t = capture.startedAt ?? Date()
                    // Only ticks in the foreground; TimelineView is suspended when the app is not visible.
                    TimelineView(.periodic(from: t, by: 1)) { ctx in
                        Text(elapsed(ctx.date.timeIntervalSince(t))).monospacedDigit()
                    }
                    LevelMeter(capture: capture)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 200)
            .background(capture.isOn ? Color.red.opacity(0.85) : Color.gray.opacity(0.25))
            .foregroundStyle(capture.isOn ? .white : .primary)
            .clipShape(RoundedRectangle(cornerRadius: 28))
        }
    }

    /// One toggle: on = upload when the Mac is reachable; off = keep recordings on the phone only (saves power outside).
    private var autoUploadButton: some View {
        let on = mode == UploadMode.auto.rawValue
        return Button {
            mode = on ? UploadMode.record.rawValue : UploadMode.auto.rawValue
            queue.modeChanged()
        } label: {
            Label(on ? "自动上传：开" : "自动上传：关（只存手机）", systemImage: on ? "icloud.fill" : "icloud.slash")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .tint(on ? .blue : .gray)
    }

    /// Instant on-device transcript for the review screen; the Mac transcript stays the authoritative one.
    private var phoneAsrToggle: some View {
        Toggle(isOn: $useOnDevice) {
            VStack(alignment: .leading, spacing: 2) {
                Text("手机预转写")
                Text("录完直接用手机转，等 Mac 校对再对比；首次开启下载模型（Wi-Fi 下）").font(.caption).foregroundStyle(.secondary)
                // Always surface the status line when the toggle is on — users were seeing an empty box without
                // knowing the model hadn't downloaded yet.
                if useOnDevice { Text(asr.line.isEmpty ? "等待下载/载入模型…" : asr.line).font(.caption2).foregroundStyle(.secondary) }
            }
        }
        // userInitiated=true: when the user toggles it on, force-download even on cellular (their choice).
        .onChange(of: useOnDevice) { _, on in if on { PhoneASR.shared.warmUp(userInitiated: true) } }
    }

    private var stats: some View {
        VStack(alignment: .leading, spacing: 6) {
            row("Engine", capture.running ? "running" : (capture.isOn ? "NOT running (retrying)" : "idle"))
            row("Segments", "\(capture.segmentCount)")
            row("Pending uploads", "\(queue.pending.count)")
            if queue.quarantined > 0 { row("Quarantined", "\(queue.quarantined)") }
            row("Last event", capture.lastEvent.isEmpty ? "-" : capture.lastEvent)
            if let e = capture.lastError ?? queue.lastError { Text(e).font(.footnote).foregroundStyle(.red) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ k: String, _ v: String) -> some View {
        HStack { Text(k).foregroundStyle(.secondary); Spacer(); Text(v) }.font(.callout)
    }

    private func elapsed(_ s: TimeInterval) -> String {
        let i = Int(s)
        return String(format: "%02d:%02d:%02d", i / 3600, (i / 60) % 60, i % 60)
    }
}

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject var queue: UploadQueue
    @State private var profiles = Settings.profiles
    @State private var active = Settings.activeProfile
    @State private var editing: Settings.Profile?     // original of the profile being edited (name is its key)
    @State private var showEditor = false
    @State private var draftName = ""
    @State private var draftURL = ""
    @AppStorage(Settings.segmentSecondsKey) private var seg = 60
    @AppStorage(Settings.skipSilenceKey) private var skip = false
    @State private var token = Settings.token
    @State private var health = ""
    @AppStorage(Settings.benchLocalASRKey) private var benchLocal = false
    @AppStorage(Settings.phoneAsrModelKey) private var asrModel = "base"
    @ObservedObject private var asr = PhoneASRStatus.shared

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    ForEach(profiles) { p in
                        Button { select(p.name) } label: {
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(p.name).foregroundStyle(.primary)
                                    Text(p.url).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if p.name == (profiles.contains { $0.name == active } ? active : profiles.first?.name) {
                                    Image(systemName: "checkmark").foregroundStyle(.blue)
                                }
                            }
                        }
                        .swipeActions(edge: .trailing) {
                            Button("Delete", role: .destructive) { delete(p) }
                            Button("Edit") { edit(p) }.tint(.orange)
                        }
                    }
                    Button("Add profile") { edit(nil) }
                    Text("Tap to select. The selected profile is tried first, then the others in order (3 s each). Swipe to edit or delete.").font(.footnote).foregroundStyle(.secondary)
                    SecureField("Bearer token", text: $token)
                        .onChange(of: token) { _, v in Settings.token = v.trimmingCharacters(in: .whitespacesAndNewlines) }
                    Button("Test /v1/health") { Task { await testHealth() } }
                    if !health.isEmpty { Text(health).font(.footnote) }
                }
                Section("Capture") {
                    Stepper("Segment: \(seg) s", value: $seg, in: 10...600, step: 10)
                    Toggle("Skip silent segments", isOn: $skip)
                    Text("Takes effect next time capture starts.").font(.footnote).foregroundStyle(.secondary)
                }
                Section("手机预转写（开关在主界面）") {
                    Picker("模型", selection: $asrModel) {
                        ForEach(PhoneASR.models, id: \.0) { Text($0.1).tag($0.0) }
                    }
                    .onChange(of: asrModel) { _, _ in PhoneASR.shared.warmUp(userInitiated: true) }   // no-op when the toggle is off; else downloads now
                    Text("换模型会立即下载。" + (asr.line.isEmpty ? "" : "状态：\(asr.line)")).font(.footnote).foregroundStyle(.secondary)
                }
                Section("实验") {
                    Toggle("本地转写实测", isOn: $benchLocal)
                    if benchLocal { NavigationLink("打开实测页面") { BenchView() } }
                }
            }
            .navigationTitle("Settings")
            .alert(editing == nil ? "Add profile" : "Edit profile", isPresented: $showEditor) {
                TextField("Name", text: $draftName).textInputAutocapitalization(.never).autocorrectionDisabled()
                TextField("http://host:8765", text: $draftURL)
                    .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                Button("Save") { saveDraft() }
                Button("Cancel", role: .cancel) {}
            }
            .toolbar { Button("Done") { Settings.token = token; queue.kick(resetBackoff: true); dismiss() } }
        }
    }

    private func edit(_ p: Settings.Profile?) {
        editing = p; draftName = p?.name ?? ""; draftURL = p?.url ?? ""; showEditor = true
    }

    private func persist() { Settings.profiles = profiles; Settings.activeProfile = active }

    private func select(_ name: String) { active = name; persist(); queue.kick(resetBackoff: true) }

    private func delete(_ p: Settings.Profile) {
        profiles.removeAll { $0.name == p.name }
        if active == p.name { active = profiles.first?.name ?? "" }
        persist()
    }

    private func saveDraft() {
        let n = draftName.trimmingCharacters(in: .whitespacesAndNewlines)
        let u = draftURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !n.isEmpty, Settings.parseBase(u) != nil else { health = "Profile not saved: need a name and a valid URL"; return }
        if let old = editing, let i = profiles.firstIndex(where: { $0.name == old.name }) {
            profiles[i] = .init(name: n, url: u)
            if active == old.name { active = n }
        } else if let i = profiles.firstIndex(where: { $0.name == n }) {
            profiles[i].url = u
        } else {
            profiles.append(.init(name: n, url: u))
            if active.isEmpty { active = n }
        }
        persist()
    }

    private func testHealth() async {
        let bases = Settings.baseURLs()
        guard !bases.isEmpty else { health = "No valid URL"; return }
        var lines: [String] = []
        for base in bases {
            var req = URLRequest(url: base.appendingPathComponent("v1/health"))
            req.timeoutInterval = 5
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            do {
                let (d, r) = try await URLSession.shared.data(for: req)
                let body = String(data: d, encoding: .utf8) ?? ""
                lines.append("\(base.host ?? "?"): HTTP \((r as? HTTPURLResponse)?.statusCode ?? -1) \(body.prefix(80))")
            } catch { lines.append("\(base.host ?? "?"): failed \(error.localizedDescription)") }
        }
        health = lines.joined(separator: "\n")
    }
}

struct QueueView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject var queue: UploadQueue

    var body: some View {
        NavigationStack {
            List {
                Section("Summary") {
                    Text("Pending: \(queue.pending.count)   Quarantined: \(queue.quarantined)")
                    if let t = queue.lastSuccess { Text("Last ACK: \(t.formatted(date: .omitted, time: .standard))") }
                    if let e = queue.lastError { Text(e).foregroundStyle(.red) }
                }
                Section("Pending (oldest first)") {
                    ForEach(queue.pending) { p in
                        VStack(alignment: .leading) {
                            Text(p.id).font(.caption.monospaced())
                            Text("\(p.source) · \(p.bytes / 1024) KB · attempts \(p.attempts)").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                }
            }
            .navigationTitle("Upload queue")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) { Button("Retry now") { queue.kick(resetBackoff: true) } }
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
        }
    }
}

/// Live input waveform: bars of the last ~8 s of per-buffer RMS. Exists only while recording and the app is visible
/// (parent checks `phase == .active`); redrawn ~10x/s, nothing runs when the screen is off or the app is backgrounded.
struct LevelMeter: View {
    let capture: CaptureEngine

    var body: some View {
        TimelineView(.animation(minimumInterval: 0.1)) { _ in
            let lv = capture.levels()
            HStack(alignment: .center, spacing: 3) {
                ForEach(lv.indices, id: \.self) { i in
                    Capsule().frame(width: 3, height: Self.height(lv[i]))
                }
            }
            .frame(height: 44)
            .opacity(0.9)
            .accessibilityHidden(true)
        }
    }

    /// RMS -> bar height on a dB scale: -60 dBFS (silence) = 3 pt, 0 dBFS = 44 pt.
    static func height(_ rms: Float) -> CGFloat {
        let db = 20 * log10(max(rms, 1e-6))
        let x = min(max((db + 60) / 60, 0), 1)
        return 3 + CGFloat(x) * 41
    }
}

