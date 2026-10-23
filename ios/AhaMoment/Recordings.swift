import Foundation

/// One closed chunk (m4a + sidecar) in queue/ (not yet uploaded) or recordings/ (uploaded).
struct RecChunk: Identifiable {
    let meta: SegmentMeta
    let url: URL
    let uploaded: Bool
    var id: String { meta.id }
    var recordedAt: Date { Fmt.parse(meta.recordedAt) ?? .distantPast }
}

/// A recording as the user sees it: one start->stop run.
struct RecSession: Identifiable {
    let id: String
    let start: Date
    let chunks: [RecChunk]      // by recordedAt
    var durationMs: Int { chunks.reduce(0) { $0 + $1.meta.durationMs } }
    var uploadedCount: Int { chunks.filter { $0.uploaded }.count }
}

enum RecordingStore {
    static func loadChunks() -> [RecChunk] {
        let fm = FileManager.default
        var byId = [String: RecChunk]()
        for dir in [Dirs.recordings, Dirs.queue] {           // recordings first: wins if a crash left a duplicate
            for n in ((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []) where n.hasSuffix(".json") {
                guard let d = try? Data(contentsOf: dir.appendingPathComponent(n)),
                      let m = try? JSONDecoder().decode(SegmentMeta.self, from: d), byId[m.id] == nil else { continue }
                let audio = Dirs.audio(in: dir, id: m.id)
                guard fm.fileExists(atPath: audio.path) else { continue }
                byId[m.id] = RecChunk(meta: m, url: audio, uploaded: dir == Dirs.recordings || m.uploaded == true)
            }
        }
        return Array(byId.values)
    }

    /// Newest first. Chunks without session fields (old builds) form one-chunk sessions.
    static func sessions() -> [RecSession] {
        var groups = [String: (start: Date, chunks: [RecChunk])]()
        for c in loadChunks() {
            let m = c.meta
            let start = Fmt.parse(m.sessionStart) ?? c.recordedAt
            var g = groups[m.sessionId ?? m.id] ?? (start, [])
            g.start = min(g.start, start); g.chunks.append(c)
            groups[m.sessionId ?? m.id] = g
        }
        return groups.map { RecSession(id: $0.key, start: $0.value.start,
                                       chunks: $0.value.chunks.sorted { $0.recordedAt < $1.recordedAt }) }
            .sorted { $0.start > $1.start }
    }

    /// Delete uploaded recordings older than Settings.retentionDays. Call on launch (background queue).
    static func prune() {
        let fm = FileManager.default
        let cutoff = Date().addingTimeInterval(-Double(Settings.retentionDays) * 86400)
        for n in ((try? fm.contentsOfDirectory(atPath: Dirs.recordings.path)) ?? []) {
            let u = Dirs.recordings.appendingPathComponent(n)
            guard n.hasSuffix(".json") || Dirs.audioExts.contains((n as NSString).pathExtension) else { continue }
            var date = (try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? Date()
            if n.hasSuffix(".json"), let d = try? Data(contentsOf: u),
               let m = try? JSONDecoder().decode(SegmentMeta.self, from: d), let t = Fmt.parse(m.recordedAt) { date = t }
            if date < cutoff { try? fm.removeItem(at: u) }
        }
        for n in ((try? fm.contentsOfDirectory(atPath: Dirs.phone.path)) ?? []) {      // phone-side transcript previews
            let u = Dirs.phone.appendingPathComponent(n)
            if let d = try? u.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate, d < cutoff { try? fm.removeItem(at: u) }
        }
    }
}
