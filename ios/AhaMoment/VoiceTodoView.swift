import SwiftUI
import EventKit

struct VoiceEvidence: Codable, Hashable { var source: String; var para: Int?; var quote: String }
struct VoiceTask: Codable, Identifiable, Hashable {
    var id: String; var title: String; var note: String?; var next: String?
    var area: String?; var topic: String?; var evidence: [VoiceEvidence]?
    var status: String?; var changeId: String?; var claudeUuid: String?; var created: String?; var updated: String?
    var state: String { status ?? "open" }
}
struct SynthStatus: Codable { var state: String?; var finished: String?; var lastOk: String?; var error: String? }
struct VoiceTasksResp: Codable { var items: [VoiceTask]; var synth: SynthStatus? }

@MainActor
final class VoiceTodoModel: ObservableObject {
    @Published var items: [VoiceTask] = []     // every non-dismissed task (open / reminded / claude)
    @Published var synth: SynthStatus?
    @Published var loaded = false
    @Published var busy = false
    @Published var message: String?

    var open: [VoiceTask] { items.filter { $0.state == "open" } }

    private let listName = "Aha 待确认"
    private let unackedKey = "aha_saved_unacked_ids"
    private let store = EKEventStore()

    private var unacked: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: unackedKey) ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: unackedKey) }
    }

    func load() async {
        await retryAck()
        do {
            let d = try await PanelAPI.request("v1/tasks")
            let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
            let r = try dec.decode(VoiceTasksResp.self, from: d)
            let pending = unacked
            items = r.items.filter { $0.state != "dismissed" }.map { t in
                var t = t; if pending.contains(t.id) && t.state == "open" { t.status = "reminded" }; return t
            }
            synth = r.synth; loaded = true
        } catch { message = "加载失败：\(error.localizedDescription)" }
    }

    private func setStatus(_ ids: Set<String>, _ s: String) {
        items = items.map { var t = $0; if ids.contains(t.id) { t.status = s }; return t }
    }

    /// Ack ids already saved to Reminders but not yet acked (previous ack failed).
    private func retryAck() async {
        let ids = unacked
        guard !ids.isEmpty else { return }
        if await ack(Array(ids)) { unacked = [] }
    }

    private func ack(_ ids: [String]) async -> Bool {
        guard let body = try? JSONSerialization.data(withJSONObject: ["ids": ids]) else { return false }
        return (try? await PanelAPI.request("v1/tasks/ack", method: "POST", body: body)) != nil
    }

    private func requestAccess() async -> Bool {
        do {
            if #available(iOS 17.0, *) { return try await store.requestFullAccessToReminders() }
            return try await store.requestAccess(to: .reminder)
        } catch { return false }
    }

    private func targetCalendar() throws -> EKCalendar {
        if let c = store.calendars(for: .reminder).first(where: { $0.title == listName }) { return c }
        let c = EKCalendar(for: .reminder, eventStore: store)
        c.title = listName
        guard let src = store.defaultCalendarForNewReminders()?.source
                ?? store.sources.first(where: { $0.sourceType == .calDAV })
                ?? store.sources.first(where: { $0.sourceType == .local }) else {
            throw NSError(domain: "aha", code: 1, userInfo: [NSLocalizedDescriptionKey: "找不到提醒事项账户"])
        }
        c.source = src
        try store.saveCalendar(c, commit: true)
        return c
    }

    func push() async { await push(open) }

    /// Save `batch` into the Reminders list, persist ids, ack, and drop them from the screen.
    /// Used by the bulk button (all items) and by swipe-right (one item) — same EventKit path + ack.
    func push(_ batch: [VoiceTask]) async {
        guard !busy, !batch.isEmpty else { return }
        busy = true; message = nil
        defer { busy = false }
        guard await requestAccess() else { message = "没有提醒事项权限，请在 设置 → Aha 里开启"; return }
        let cal: EKCalendar
        do { cal = try targetCalendar() } catch { message = "创建列表失败：\(error.localizedDescription)"; return }
        var saved: [String] = []
        for it in batch {
            let r = EKReminder(eventStore: store)
            r.title = it.title
            r.notes = it.note
            r.calendar = cal
            do { try store.save(r, commit: false); saved.append(it.id) } catch {}
        }
        do { try store.commit() } catch {
            store.reset(); message = "保存失败：\(error.localizedDescription)"; return
        }
        unacked.formUnion(saved)   // persist before ack: never re-add if ack fails
        let ok = await ack(Array(unacked))
        if ok { unacked = [] }
        let failed = batch.count - saved.count
        message = "已添加 \(saved.count) 条" + (failed > 0 ? "，\(failed) 条失败" : "") + (ok ? "" : "（回执失败，下次自动重试）")
        let done = Set(saved)
        setStatus(done, "reminded")
    }

    /// Swipe-left: tell the server to dismiss (never shown again); remove locally only if the server confirmed.
    func dismiss(_ it: VoiceTask) async {
        guard let body = try? JSONSerialization.data(withJSONObject: ["ids": [it.id]]),
              (try? await PanelAPI.request("v1/tasks/dismiss", method: "POST", body: body)) != nil else {
            message = "删除失败，请稍后重试"; return
        }
        items.removeAll { $0.id == it.id }
    }

    /// User says "I did it": POST /v1/tasks/done; drop from the screen on success.
    func markDone(_ it: VoiceTask) async {
        guard let body = try? JSONSerialization.data(withJSONObject: ["ids": [it.id]]),
              (try? await PanelAPI.request("v1/tasks/done", method: "POST", body: body)) != nil else {
            message = "标完成失败，请稍后重试"; return
        }
        items.removeAll { $0.id == it.id }
    }

    /// Hand the task to a non-interactive Claude run (server starts it; status becomes `claude`).
    func toClaude(_ it: VoiceTask) async {
        do {
            _ = try await PanelAPI.request("v1/tasks/\(it.id)/to_claude", method: "POST", body: Data("{}".utf8))
            setStatus([it.id], "claude"); message = "已交给 Claude（只写 proposal）"
        } catch { message = "交给 Claude 失败：\(error.localizedDescription)" }
    }

    /// Kick off a voice-task resynthesis on the server (returns immediately; poll `synth.state`).
    func startResynthesize() async {
        _ = try? await PanelAPI.request("v1/tasks/resynthesize", method: "POST", body: Data("{}".utf8))
    }
}


/// Detail of one voice task: next step, evidence quotes (tap shows the source file), and the three actions.
struct VoiceTaskDetail: View {
    let id: String
    @EnvironmentObject var m: VoiceTodoModel
    @Environment(\.dismiss) private var dismiss
    @State private var showSource: Set<Int> = []
    var body: some View {
        if let t = m.items.first(where: { $0.id == id }) {
            List {
                Section {
                    Text(t.title).font(.headline)
                    if let n = t.next, !n.isEmpty { Text(n).foregroundStyle(.secondary) }
                    HStack {
                        if let a = t.area { Text(a) }
                        if let tp = t.topic { Text("· \(tp)") }
                        Spacer()
                        Text(statusText(t.state)).foregroundStyle(.secondary)
                    }.font(.footnote).foregroundStyle(.secondary)
                }
                Section("证据（语音原话）") {
                    ForEach(Array((t.evidence ?? []).enumerated()), id: \.offset) { i, e in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(e.quote).font(.callout)
                            if showSource.contains(i) { Text(e.source + (e.para.map { " #\($0)" } ?? "")).font(.caption2).foregroundStyle(.secondary) }
                        }
                        .contentShape(Rectangle())
                        .onTapGesture { if showSource.contains(i) { showSource.remove(i) } else { showSource.insert(i) } }
                    }
                }
                Section {
                    Button { Task { await m.push([t]); dismiss() } } label: { Label("推到提醒事项", systemImage: "checklist") }
                        .disabled(t.state != "open" || m.busy)
                    Button { Task { await m.toClaude(t); dismiss() } } label: { Label("交给 Claude", systemImage: "sparkles") }
                        .disabled(t.state == "claude")
                    Button { Task { await m.markDone(t); dismiss() } } label: { Label("完成", systemImage: "checkmark.circle") }
                        .disabled(t.state == "done")
                    Button(role: .destructive) { Task { await m.dismiss(t); dismiss() } } label: { Label("删除", systemImage: "trash") }
                    if let msg = m.message { Text(msg).font(.footnote).foregroundStyle(.secondary) }
                }
            }
            .navigationTitle("语音任务").navigationBarTitleDisplayMode(.inline)
        } else {
            Text("任务已不存在").foregroundStyle(.secondary)
        }
    }
    private func statusText(_ s: String) -> String {
        switch s {
        case "reminded": return "已推到提醒事项"
        case "claude": return "已交给 Claude"
        case "done": return "已完成"
        default: return "待处理"
        }
    }
}
