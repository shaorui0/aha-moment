import AVFoundation
import SwiftUI

private func clock(_ s: Double) -> String {
    let i = Int(max(0, s))
    return i >= 3600 ? String(format: "%d:%02d:%02d", i / 3600, (i / 60) % 60, i % 60) : String(format: "%d:%02d", i / 60, i % 60)
}

@MainActor
final class RecordingsModel: ObservableObject {
    @Published private(set) var sessions: [RecSession] = []
    func reload() async {
        sessions = await Task.detached { RecordingStore.sessions() }.value
    }
}

struct RecordingsView: View {
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject var capture: CaptureEngine
    @EnvironmentObject var queue: UploadQueue
    @StateObject private var model = RecordingsModel()
    @State private var claudeSent = Settings.claudeSessions
    @State private var sent = Set<String>()   // tapped this screen visit; icon shows "sending" until all chunks uploaded

    var body: some View {
        NavigationStack {
            List {
                if let st = queue.priorityStatus {
                    Text(st).font(.footnote).foregroundStyle(st.hasPrefix("发送失败") ? .red : .secondary)
                }
                ForEach(model.sessions) { s in
                    HStack(spacing: 12) {
                        uploadButton(s)
                        claudeButton(s)
                        NavigationLink { ReviewView(session: s) } label: { row(s) }
                    }
                }
            }
            .overlay { if model.sessions.isEmpty { Text("No recordings").foregroundStyle(.secondary) } }
            .navigationTitle("Recordings")
            .onAppear { claudeSent = Settings.claudeSessions }   // a send from the review screen
            .toolbar { ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } } }
            .task {
                while !Task.isCancelled {
                    await model.reload()
                    try? await Task.sleep(nanoseconds: 5_000_000_000)
                }
            }
        }
    }

    /// Cloud button left of each recording: upload this recording now (priority transcription on the Mac).
    private func uploadButton(_ s: RecSession) -> some View {
        let done = s.uploadedCount == s.chunks.count && !capture.liveSessionIds.contains(s.id)
        let sending = sent.contains(s.id) && !done
        return Button {
            sent.insert(s.id)
            capture.sendSession(s.id)
        } label: {
            Image(systemName: done ? "checkmark.icloud" : "icloud.and.arrow.up")
                .font(.title3)
                .foregroundStyle(done ? Color.green : (sending ? Color.orange : Color.accentColor))
                .symbolEffect(.pulse, isActive: sending)
                .frame(width: 32, height: 32)
        }
        .buttonStyle(.borderless)   // keep the tap from triggering the row's NavigationLink
        .disabled(done)
        .accessibilityLabel(done ? "已上传" : "上传这条录音")
    }

    /// Sparkles button: send this recording's transcript to Claude Code (tag once; stays highlighted).
    private func claudeButton(_ s: RecSession) -> some View {
        let tagged = claudeSent.contains(s.id)
        return Button {
            claudeSent.insert(s.id)
            queue.tagSession(s.id)
        } label: {
            Image(systemName: "sparkles")
                .font(.title3)
                .foregroundStyle(tagged ? Color.purple : Color.secondary)
                .frame(width: 32, height: 32)
        }
        .buttonStyle(.borderless)
        .disabled(tagged)
        .accessibilityLabel(tagged ? "已发给 Claude Code" : "发给 Claude Code")
    }

    private func row(_ s: RecSession) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text(s.start.formatted(date: .abbreviated, time: .shortened)).font(.headline)
            }
            HStack {
                Text(clock(Double(s.durationMs) / 1000)).monospacedDigit()
                if capture.liveSessionIds.contains(s.id) { Text("Recording…").foregroundStyle(.red) }
                if claudeSent.contains(s.id) { Text("Claude").foregroundStyle(.purple) }
                Spacer()
                Text("\(s.uploadedCount)/\(s.chunks.count) uploaded").foregroundStyle(.secondary)
            }.font(.caption)
        }
    }
}

/// Plays all chunks of a session as one AVMutableComposition (single timeline). The composition is rebuilt when the chunk
/// list changes (new chunk closed, or a chunk moved queue/ -> recordings/ after upload), keeping position and play state.
@MainActor
final class PlayerModel: ObservableObject {
    @Published private(set) var isPlaying = false
    @Published var position: Double = 0
    @Published private(set) var total: Double = 0
    @Published var scrubbing = false

    let sessionId: String
    private let player = AVPlayer()
    private var signature = ""
    private var timeObs: Any?
    private var endObs: NSObjectProtocol?

    init(sessionId: String) { self.sessionId = sessionId }

    func start() {
        guard timeObs == nil else { return }
        timeObs = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] t in
            MainActor.assumeIsolated {
                guard let self, !self.scrubbing, t.isNumeric else { return }
                self.position = min(t.seconds, self.total)
            }
        }
        endObs = NotificationCenter.default.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] n in
            MainActor.assumeIsolated {
                if let self, (n.object as? AVPlayerItem) === self.player.currentItem { self.isPlaying = false }
            }
        }
    }

    func stop() {
        player.pause(); isPlaying = false
        if let t = timeObs { player.removeTimeObserver(t); timeObs = nil }
        if let e = endObs { NotificationCenter.default.removeObserver(e); endObs = nil }
    }

    func refresh() async {
        let id = sessionId
        guard let sess = await Task.detached(operation: { RecordingStore.sessions().first { $0.id == id } }).value else { return }
        let sig = sess.chunks.map { $0.url.path }.joined(separator: "|")
        guard sig != signature else { return }
        let comp = AVMutableComposition()
        guard let track = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid) else { return }
        var cursor = CMTime.zero
        var failed = false
        for c in sess.chunks {
            let a = AVURLAsset(url: c.url)
            do {
                let d = try await a.load(.duration)
                guard let tr = try await a.loadTracks(withMediaType: .audio).first else { failed = true; continue }
                try track.insertTimeRange(CMTimeRange(start: .zero, duration: d), of: tr, at: cursor)
                cursor = CMTimeAdd(cursor, d)
            } catch { failed = true }     // e.g. file moved after upload between listing and loading; retried next tick
        }
        signature = failed ? "" : sig
        let resume = isPlaying, t = position
        player.replaceCurrentItem(with: AVPlayerItem(asset: comp))
        total = cursor.seconds
        if t > 0 { await player.seek(to: CMTime(seconds: min(t, total), preferredTimescale: 600)) }
        if resume { player.play() }
    }

    func toggle(captureRunning: Bool) {
        if isPlaying { player.pause(); isPlaying = false; return }
        guard total > 0 else { return }
        // While capturing, the session is already .playAndRecord (+mixWithOthers, defaultToSpeaker): use it untouched and never deactivate.
        if !captureRunning { try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: [.mixWithOthers]) }
        if position >= total - 0.05 { seek(to: 0) }
        player.play(); isPlaying = true
    }

    func seek(to s: Double) {
        position = s
        player.seek(to: CMTime(seconds: s, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
    }
}

struct PlayerView: View {
    @EnvironmentObject var capture: CaptureEngine
    let session: RecSession
    @StateObject private var model: PlayerModel

    init(session: RecSession) {
        self.session = session
        _model = StateObject(wrappedValue: PlayerModel(sessionId: session.id))
    }

    var body: some View {
        VStack(spacing: 24) {
            Text(session.start.formatted(date: .complete, time: .shortened)).font(.headline)
            if capture.liveSessionIds.contains(session.id) { Text("Recording…").foregroundStyle(.red) }
            Spacer()
            Slider(value: Binding(get: { model.position }, set: { model.position = $0 }),
                   in: 0...max(model.total, 1)) { editing in
                if editing { model.scrubbing = true } else { model.seek(to: model.position); model.scrubbing = false }
            }
            HStack {
                Text(clock(model.position)); Spacer(); Text(clock(model.total))
            }.font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            Button { model.toggle(captureRunning: capture.running) } label: {
                Image(systemName: model.isPlaying ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 72))
            }
            Spacer()
        }
        .padding()
        .navigationTitle("Recording")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear { model.start() }
        .onDisappear { model.stop() }
        .task {
            while !Task.isCancelled {
                await model.refresh()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }
}
