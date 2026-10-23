import SwiftUI

// MARK: - Tabs

/// Bottom tab bar: 录音 (existing main screen) + 面板 (read-only Claude monitoring).
struct RootView: View {
    var body: some View {
        TabView {
            ContentView().tabItem { Label("录音", systemImage: "mic.fill") }
            PanelView().tabItem { Label("面板", systemImage: "rectangle.stack") }
        }
    }
}

// MARK: - Models (server: /v1/claude/*, snake_case)

struct UsageWindow: Codable { var id: String; var percent: Double }
struct Usage: Codable { var windows: [UsageWindow]? }

struct TaskItem: Codable, Hashable { var text: String; var done: Bool; var current: Bool? }
struct Progress: Codable, Hashable { var done: Int; var total: Int }
struct ClaudeSession: Codable, Identifiable {
    var sid: String; var title: String; var state: String
    var waitingFor: String?; var since: String?; var updated: String?
    var progress: Progress?; var tasks: [TaskItem]
    var lastUser: String; var lastAssistant: String
    var id: String { sid }
}
struct SessionsResp: Codable { var sessions: [ClaudeSession] }

struct ChangeRow: Codable, Identifiable { var id: String; var title: String; var done: Int; var total: Int; var path: String }
struct TaskGroup: Codable, Identifiable { var area: String; var group: String; var changes: [ChangeRow]; var id: String { area + "/" + group } }
struct Regroup: Codable { var state: String?; var error: String? }
struct TasksResp: Codable { var generatedAt: String?; var groups: [TaskGroup]; var regroup: Regroup? }

struct AhaRun: Codable, Identifiable {
    var uuid: String; var name: String; var kind: String?; var status: String
    var started: String?; var finished: String?; var title: String; var changeId: String?; var proposalPath: String?
    var id: String { uuid + name }
}
struct RunsResp: Codable { var runs: [AhaRun] }

/// /v1/todos: one row is either an OpenSpec change (kind "openspec") or a voice-synthesized task (kind "voice_task").
struct TodoRow: Codable, Identifiable, Hashable {
    var kind: String; var id: String; var title: String; var source: String?
    var done: Int?; var total: Int?; var percent: Int?; var changePath: String?; var runName: String?; var started: String?
    var next: String?; var area: String?; var topic: String?; var status: String?; var changeId: String?; var created: String?
}
struct TodoGroup: Codable, Identifiable { var source: String; var items: [TodoRow]; var id: String { source } }
struct TodoSummary: Codable { var totalOpen: Int; var totalDoneToday: Int }
struct TodosResp: Codable { var generatedAt: String?; var groups: [TodoGroup]; var summary: TodoSummary }

/// Value-based navigation: every row pushes one of these onto the single NavigationStack owned by PanelView.
enum PanelRoute: Hashable {
    case session(sid: String, title: String)
    case change(id: String, title: String)
    case run(uuid: String, name: String)
    case voice(id: String)
}

struct ChecklistItem: Codable, Hashable { var text: String; var done: Bool }
struct ChangeLink: Codable, Hashable { var id: String; var title: String }
struct ChatMsg: Codable, Hashable { var role: String; var text: String; var at: String? }
struct SessionDetailResp: Codable {
    var sid: String; var title: String; var state: String
    var waitingFor: String?; var since: String?; var updated: String?; var started: String?; var model: String?
    var progress: Progress?; var tasks: [TaskItem]; var workspace: String
    var messages: [ChatMsg]; var changeLinks: [ChangeLink]
}
struct ChangeDetailResp: Codable { var id: String; var title: String; var proposal: String; var truncated: Bool; var tasks: [ChecklistItem]; var done: Int; var total: Int }
struct RunDetailResp: Codable {
    var uuid: String; var name: String; var recordingId: String?; var kind: String?; var status: String
    var started: String?; var finished: String?; var transcript: String
    var changeId: String?; var proposalPath: String?; var proposal: String?; var logTail: [String]
}

// MARK: - Networking + disk cache

enum PanelAPI {
    static func cacheURL(_ key: String) -> URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("panel_\(key).json")
    }

    /// Try each configured base URL; first success wins.
    static func request(_ path: String, method: String = "GET", body: Data? = nil, timeout: TimeInterval = 8) async throws -> Data {
        var last: Error = URLError(.cannotConnectToHost)
        for base in Settings.baseURLs() {
            // appendingPathComponent would percent-encode "?" in paths like "v1/digest?kind=aha" -> 404
            guard let url = URL(string: base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/" + path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))) else { continue }
            var req = URLRequest(url: url, timeoutInterval: timeout)
            req.httpMethod = method
            if let body { req.httpBody = body; req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
            req.setValue("Bearer \(Settings.token)", forHTTPHeaderField: "Authorization")
            do {
                let (d, r) = try await URLSession.shared.data(for: req)
                let code = (r as? HTTPURLResponse)?.statusCode ?? 0
                guard (200..<300).contains(code) else {
                    last = NSError(domain: "aha", code: code, userInfo: [NSLocalizedDescriptionKey: "HTTP \(code)"]); continue
                }
                return d
            } catch { last = error }
        }
        throw last
    }
}

@MainActor
final class PanelStore<T: Codable>: ObservableObject {
    let path: String, key: String
    @Published var value: T?
    @Published var fetchedAt: Date?
    @Published var offline = false
    @Published var error: String?
    init(_ path: String, key: String) {
        self.path = path; self.key = key
        let u = PanelAPI.cacheURL(key)
        if let d = try? Data(contentsOf: u), let v = try? Self.decode(d) {
            value = v; fetchedAt = (try? u.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate; offline = true
        }
    }
    static func decode(_ d: Data) throws -> T {
        let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
        return try dec.decode(T.self, from: d)
    }
    func load() async {
        do {
            let d = try await PanelAPI.request(path)
            value = try Self.decode(d); fetchedAt = Date(); offline = false; error = nil
            try? d.write(to: PanelAPI.cacheURL(key), options: .atomic)
        } catch { offline = true; self.error = error.localizedDescription }
    }
}

// MARK: - Helpers

private func parseDate(_ s: String?) -> Date? {
    guard let s else { return nil }
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f.date(from: s) ?? ISO8601DateFormatter().date(from: s)
}

private func ago(_ d: Date?) -> String {
    guard let d else { return "" }
    let s = Int(Date().timeIntervalSince(d))
    if s < 60 { return "刚刚" }
    if s < 3600 { return "\(s / 60) 分钟前" }
    if s < 86400 { return "\(s / 3600) 小时前" }
    return "\(s / 86400) 天前"
}

private struct StaleBanner: View {
    let offline: Bool, at: Date?, error: String?
    var body: some View {
        if offline {
            Text("离线 · 上次更新 \(ago(at))" + (error.map { " · \($0)" } ?? "")).font(.footnote).foregroundStyle(.orange)
        } else if let at {
            Text("更新于 \(ago(at))").font(.footnote).foregroundStyle(.secondary)
        }
    }
}

// MARK: - Container

struct PanelView: View {
    enum Tab: String, CaseIterable { case sessions = "Sessions", tasks = "Tasks", todos = "待办", aha = "Aha", jobs = "Jobs", digest = "日报" }
    @State private var tab = Tab.sessions
    @StateObject private var voice = VoiceTodoModel()
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                Picker("", selection: $tab) { ForEach(Tab.allCases, id: \.self) { Text($0.rawValue) } }
                    .pickerStyle(.segmented).padding(.horizontal).padding(.vertical, 8)
                switch tab {
                case .sessions: SessionsTab()
                case .tasks: TasksTab()
                case .todos: TodosTab()
                case .aha: RunsTab()
                case .jobs: JobsTab()
                case .digest: DigestTab()
                }
            }
            .navigationTitle("面板").navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: PanelRoute.self) { route in
                switch route {
                case .session(let sid, let title): SessionDetail(sid: sid, title: title)
                case .change(let id, let title): ChangeDetail(id: id, title: title)
                case .run(let uuid, let name): RunDetail(uuid: uuid, name: name)
                case .voice(let id): VoiceTaskDetail(id: id)
                }
            }
        }
        .environmentObject(voice)
    }
}

// MARK: - Sessions

private struct UsageBars: View {
    let usage: Usage?
    var body: some View {
        VStack(spacing: 6) {
            bar("5 小时", "five_hour"); bar("本周", "seven_day")
        }
    }
    private func bar(_ label: String, _ id: String) -> some View {
        let p = usage?.windows?.first { $0.id == id }?.percent
        return HStack {
            Text(label).font(.footnote).frame(width: 52, alignment: .leading)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.gray.opacity(0.2))
                    Capsule().fill((p ?? 0) >= 90 ? Color.red : (p ?? 0) >= 70 ? Color.orange : Color.green)
                        .frame(width: g.size.width * min(1, (p ?? 0) / 100))
                    ForEach([70.0, 90.0], id: \.self) { t in
                        Rectangle().fill(Color.primary.opacity(0.45)).frame(width: 1.5)
                            .offset(x: g.size.width * t / 100)
                    }
                }
            }.frame(height: 8)
            Text(p.map { "\(Int($0))%" } ?? "—").font(.footnote.monospacedDigit()).frame(width: 40, alignment: .trailing)
        }
    }
}

private struct SessionsTab: View {
    @StateObject private var s = PanelStore<SessionsResp>("v1/claude/sessions", key: "sessions")
    @StateObject private var u = PanelStore<Usage>("v1/claude/usage", key: "usage")
    private let groups: [(String, String)] = [("running", "运行中"), ("needs_input", "停下等你"), ("stale", "停滞"), ("done", "今天结束")]
    var body: some View {
        List {
            Section {
                UsageBars(usage: u.value)
                StaleBanner(offline: s.offline, at: s.fetchedAt, error: s.error)
            }
            ForEach(groups, id: \.0) { st, name in
                let rows = (s.value?.sessions ?? []).filter { $0.state == st }
                if !rows.isEmpty {
                    Section("\(name) · \(rows.count)") {
                        ForEach(rows) { r in NavigationLink(value: PanelRoute.session(sid: r.sid, title: r.title)) { row(r) } }
                    }
                }
            }
        }
        .refreshable { await refresh() }
        .task { await refresh() }
    }
    private func refresh() async { async let a: Void = s.load(); async let b: Void = u.load(); _ = await (a, b) }
    private func row(_ r: ClaudeSession) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(r.title).lineLimit(1)
                Text(r.state == "needs_input" ? (r.waitingFor ?? "在等你回答") : (r.progress.map { "\($0.done)/\($0.total) 任务" } ?? ""))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer()
            Text(r.state == "running" ? "now" : ago(parseDate(r.since ?? r.updated)).replacingOccurrences(of: "前", with: ""))
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

private let stateNames = ["running": "运行中", "needs_input": "停下等你", "stale": "停滞", "done": "已结束"]

/// Shared loading/empty shell for the three detail screens (cached last response, offline banner).
private struct DetailShell<T: Codable, Content: View>: View {
    @ObservedObject var store: PanelStore<T>
    @ViewBuilder let content: (T) -> Content
    var body: some View {
        List {
            Section { StaleBanner(offline: store.offline, at: store.fetchedAt, error: store.error) }
            if let v = store.value { content(v) }
            else if store.error == nil { Section { ProgressView() } }
            else { Section { Text("加载失败").foregroundStyle(.secondary) } }
        }
        .refreshable { await store.load() }
        .task { await store.load() }
    }
}

private func checkIcon(_ done: Bool, current: Bool = false) -> String {
    done ? "checkmark.circle.fill" : (current ? "circle.dotted" : "circle")
}

private struct SessionDetail: View {
    let title: String
    @StateObject private var d: PanelStore<SessionDetailResp>
    init(sid: String, title: String) {
        self.title = title
        _d = StateObject(wrappedValue: PanelStore<SessionDetailResp>("v1/claude/sessions/\(sid)", key: "session_\(sid)"))
    }
    var body: some View {
        DetailShell(store: d) { v in
            Section {
                LabeledContent("状态", value: stateNames[v.state] ?? v.state)
                if let w = v.waitingFor { LabeledContent("在等", value: w) }
                LabeledContent("自", value: parseDate(v.since ?? v.updated).map { $0.formatted(date: .abbreviated, time: .shortened) + "（\(ago($0))）" } ?? "—")
                if let p = v.progress { LabeledContent("进度", value: "\(p.done)/\(p.total)") }
                VStack(alignment: .leading, spacing: 2) {
                    Text("工作目录").font(.caption).foregroundStyle(.secondary)
                    Text(v.workspace).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
            if !v.changeLinks.isEmpty {
                Section("OpenSpec change") {
                    ForEach(v.changeLinks, id: \.id) { c in
                        NavigationLink(value: PanelRoute.change(id: c.id, title: c.title)) { Text(c.title).font(.callout).lineLimit(2) }
                    }
                }
            }
            if !v.tasks.isEmpty {
                Section("任务") {
                    ForEach(Array(v.tasks.enumerated()), id: \.offset) { _, t in
                        Label(t.text, systemImage: checkIcon(t.done, current: t.current == true))
                            .foregroundStyle(t.done ? .secondary : .primary).font(.callout)
                    }
                }
            }
            Section("最近消息") {
                if v.messages.isEmpty { Text("没有找到对话记录").foregroundStyle(.secondary) }
                ForEach(Array(v.messages.enumerated()), id: \.offset) { _, m in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(m.role == "user" ? "你" : "Claude").font(.caption).foregroundStyle(.secondary)
                        Text(m.text).font(.callout).foregroundStyle(m.role == "user" ? .primary : .secondary).textSelection(.enabled)
                    }
                }
            }
        }
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }
}

private struct ChangeDetail: View {
    let title: String
    @StateObject private var d: PanelStore<ChangeDetailResp>
    init(id: String, title: String) {
        self.title = title
        _d = StateObject(wrappedValue: PanelStore<ChangeDetailResp>("v1/claude/changes/\(id)", key: "change_\(id)"))
    }
    var body: some View {
        DetailShell(store: d) { v in
            Section("任务 \(v.done)/\(v.total)") {
                if v.tasks.isEmpty { Text("没有 tasks.md").foregroundStyle(.secondary) }
                ForEach(Array(v.tasks.enumerated()), id: \.offset) { _, t in
                    Label(t.text, systemImage: checkIcon(t.done)).foregroundStyle(t.done ? .secondary : .primary).font(.callout)
                }
            }
            Section("Proposal") {
                MarkdownText(md: v.proposal.isEmpty ? "（空）" : v.proposal + (v.truncated ? "\n\n…（已截断）" : ""))
            }
        }
        .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Tasks

private struct TasksTab: View {
    @StateObject private var t = PanelStore<TasksResp>("v1/claude/tasks", key: "tasks")
    @EnvironmentObject var v: VoiceTodoModel
    @State private var busy = false
    @State private var msg: String?

    /// area -> topic/group -> {voice tasks without a change, changes (each with the voice tasks linked to it)}
    private func keys(_ area: String) -> [String] {
        var ks = (t.value?.groups ?? []).filter { $0.area == area }.map { $0.group }
        for it in v.items where (it.area ?? "个人") == area && it.state != "dismissed" {
            let k = it.topic ?? "其他"; if !ks.contains(k) { ks.append(k) }
        }
        return ks
    }
    private func changes(_ area: String, _ key: String) -> [ChangeRow] {
        (t.value?.groups ?? []).filter { $0.area == area && $0.group == key }.flatMap { $0.changes }
    }
    private func loose(_ area: String, _ key: String) -> [VoiceTask] {
        let linked = Set((t.value?.groups ?? []).flatMap { $0.changes }.map { $0.id })
        return v.items.filter { ($0.area ?? "个人") == area && ($0.topic ?? "其他") == key && !($0.changeId.map { linked.contains($0) } ?? false) }
    }
    private func linked(_ c: ChangeRow) -> [VoiceTask] { v.items.filter { $0.changeId == c.id } }

    var body: some View {
        List {
            ForEach(["工作", "个人"], id: \.self) { area in
                let ks = keys(area)
                if !ks.isEmpty {
                    Section(area) {
                        ForEach(ks, id: \.self) { k in
                            let ls = loose(area, k), cs = changes(area, k)
                            DisclosureGroup {
                                ForEach(ls) { it in VoiceRow(it: it) }
                                ForEach(cs) { c in
                                    NavigationLink(value: PanelRoute.change(id: c.id, title: c.title)) {
                                        HStack {
                                            Image(systemName: "checkmark.square.fill").foregroundStyle(.tint)
                                            Text(c.id).font(.callout).lineLimit(1)
                                            Spacer()
                                            Text(c.total > 0 ? "\(c.done)/\(c.total)" : "—").font(.callout.monospacedDigit())
                                                .foregroundStyle(c.total > 0 && c.done == c.total ? .green : .secondary)
                                        }
                                    }
                                    ForEach(linked(c)) { it in VoiceRow(it: it).padding(.leading, 20) }
                                }
                            } label: {
                                HStack { Text(k); Spacer(); Text("\(ls.count) 任务 · \(cs.count) changes").font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                    }
                }
            }
            Section {
                Button { Task { await v.push() } } label: {
                    HStack { Text("推到提醒事项（\(v.open.count) 条未推送）"); if v.busy { Spacer(); ProgressView() } }
                }.disabled(v.busy || v.open.isEmpty)
                Button { Task { await regroup() } } label: {
                    HStack { Text(busy ? "整理中…（约 1–3 分钟）" : "重新整理"); if busy { Spacer(); ProgressView() } }
                }.disabled(busy)
                Text(footer).font(.footnote).foregroundStyle(msg == nil ? Color.secondary : Color.orange)
                if let m = v.message { Text(m).font(.footnote).foregroundStyle(.secondary) }
            }
        }
        .refreshable { await t.load(); await v.load() }
        .task {
            await t.load(); await v.load()
            busy = t.value?.regroup?.state == "running" || v.synth?.state == "running"
            if busy { await poll() }
        }
    }
    private var footer: String {
        if let m = msg { return m }
        let c = parseDate(t.value?.generatedAt).map { "changes 整理于 " + $0.formatted(date: .omitted, time: .shortened) } ?? "changes 还没整理过"
        let s = parseDate(v.synth?.lastOk).map { "语音任务整理于 " + $0.formatted(date: .abbreviated, time: .shortened) } ?? "语音任务还没整理过"
        return (t.offline ? "离线 · " : "") + c + " · " + s + "（用 Sonnet）"
    }
    /// Runs both: regroup (OpenSpec changes) and resynthesize (voice tasks).
    private func regroup() async {
        busy = true; msg = nil
        do { _ = try await PanelAPI.request("v1/claude/tasks/regroup", method: "POST") } catch { msg = "整理 changes 启动失败：\(error.localizedDescription)" }
        await v.startResynthesize()
        await poll()
    }
    /// Only while the user is waiting on a run they started: check every 4 s, stop when both finish.
    private func poll() async {
        for _ in 0..<90 {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            await t.load(); await v.load()
            if t.value?.regroup?.state != "running" && v.synth?.state != "running" { break }
        }
        busy = false
        if t.value?.regroup?.state == "error" { msg = "整理失败：\(t.value?.regroup?.error ?? "")" }
        else if v.synth?.state == "error" { msg = "语音任务整理失败：\(v.synth?.error ?? "")" }
    }
}

/// One voice task row: swipe right = Reminders, swipe left = delete, tap = detail.
private struct VoiceRow: View {
    let it: VoiceTask
    @EnvironmentObject var v: VoiceTodoModel
    var body: some View {
        NavigationLink(value: PanelRoute.voice(id: it.id)) {
            HStack(spacing: 8) {
                Image(systemName: it.state == "open" ? "square" : (it.state == "claude" ? "sparkles" : "bell"))
                    .foregroundStyle(it.state == "open" ? Color.primary : Color.secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(it.title).foregroundStyle(it.state == "open" ? Color.primary : Color.secondary)
                    if let n = it.next, !n.isEmpty { Text(n).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
                }
            }
        }
        .swipeActions(edge: .leading, allowsFullSwipe: true) {
            if it.state == "open" {
                Button { Task { await v.push([it]) } } label: { Label("提醒事项", systemImage: "checklist") }.tint(.blue)
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            Button(role: .destructive) { Task { await v.dismiss(it) } } label: { Label("删除", systemImage: "trash") }
        }
    }
}

// MARK: - Todos (curated "what to do next": voice-triggered changes, voice-synthesized tasks, other OpenSpec changes)

/// Thin progress bar: green when complete, blue from 50%, gray below.
private struct TodoBar: View {
    let done: Int, total: Int
    private var f: Double { total > 0 ? min(1, Double(done) / Double(total)) : 0 }
    private var color: Color { f >= 1 ? Color.green : (f >= 0.5 ? Color.blue : Color.gray) }
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.gray.opacity(0.2))
                Capsule().fill(color).frame(width: g.size.width * f)
            }
        }.frame(height: 6)
    }
}

private struct TodosTab: View {
    @StateObject private var t = PanelStore<TodosResp>("v1/todos", key: "todos")
    @EnvironmentObject var v: VoiceTodoModel

    /// Voice tasks acted on from the detail screen (done / deleted / handed to Claude) disappear before the next fetch.
    private func visible(_ r: TodoRow) -> Bool {
        guard r.kind == "voice_task", v.loaded else { return true }
        guard let it = v.items.first(where: { $0.id == r.id }) else { return false }
        return it.state == "open" || it.state == "reminded"
    }
    private func header(_ source: String, _ n: Int) -> String {
        switch source {
        case "voice_trigger": return "语音触发 · \(n) 条未完成"
        case "voice_synth": return "语音录音抽取 · \(n) 条 open"
        default: return "其他 OpenSpec · \(n) 条未完成"
        }
    }
    var body: some View {
        List {
            Section {
                if let s = t.value?.summary { Text("共 \(s.totalOpen) 条待办 · 今天完成 \(s.totalDoneToday) 条").font(.subheadline) }
                StaleBanner(offline: t.offline, at: t.fetchedAt, error: t.error)
            }
            ForEach(t.value?.groups ?? []) { g in
                let rows = g.items.filter(visible)
                if !rows.isEmpty {
                    Section(header(g.source, rows.count)) { ForEach(rows) { r in row(r) } }
                }
            }
            if let g = t.value?.groups, g.isEmpty { Section { Text("没有待办").foregroundStyle(.secondary) } }
        }
        .refreshable { await refresh() }
        .task { await refresh() }
    }
    private func refresh() async { async let a: Void = t.load(); async let b: Void = v.load(); _ = await (a, b) }

    @ViewBuilder private func row(_ r: TodoRow) -> some View {
        if r.kind == "voice_task" {
            NavigationLink(value: PanelRoute.voice(id: r.id)) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline) {
                        Text(r.title).lineLimit(2)
                        Spacer()
                        chip(r.status == "reminded" ? "已提醒" : "open", r.status == "reminded" ? Color.gray : Color.orange)
                    }
                    if let n = r.next, !n.isEmpty { Text(n).font(.caption).foregroundStyle(.secondary).lineLimit(2) }
                    HStack(spacing: 6) {
                        ForEach([r.area, r.topic].compactMap { $0 }.filter { !$0.isEmpty }, id: \.self) { chip($0, Color.blue) }
                    }
                }
            }
        } else {
            let done = r.done ?? 0, total = r.total ?? 0
            NavigationLink(value: PanelRoute.change(id: r.id, title: r.title)) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(r.title).lineLimit(2)
                    TodoBar(done: done, total: total)
                    HStack {
                        Text("\(max(0, total - done)) 条未完成")
                        Spacer()
                        Text(r.source == "voice_trigger" ? "语音 · " + ago(parseDate(r.started)) : "手动")
                        Text("\(done)/\(total)").monospacedDigit()
                    }.font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }
    private func chip(_ text: String, _ color: Color) -> some View {
        Text(text).font(.caption2).padding(.horizontal, 6).padding(.vertical, 1)
            .background(color.opacity(0.15), in: Capsule()).foregroundStyle(color)
    }
}

// MARK: - Aha runs

private func runStatusText(_ status: String, hasProposal: Bool) -> String {
    switch status {
    case "running": return "正在写 proposal"
    case "done": return hasProposal ? "proposal 写好了，等你看" : "已结束"
    case "failed": return "失败"
    case "killed": return "已停止"
    case "lost": return "已中断"
    default: return status
    }
}

private struct RunsTab: View {
    @StateObject private var r = PanelStore<RunsResp>("v1/claude/runs", key: "runs")
    @State private var arm: String?   // uuid awaiting the second tap
    @State private var msg: String?
    var body: some View {
        List {
            Section { StaleBanner(offline: r.offline, at: r.fetchedAt, error: r.error) }
            Section {
                ForEach(r.value?.runs ?? []) { run in
                    NavigationLink(value: PanelRoute.run(uuid: run.uuid, name: run.name)) {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(run.name).font(.callout).lineLimit(1)
                                Text(statusText(run)).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                            }
                            Spacer()
                            if run.status == "running" { stopButton(run) }
                            else { Text(ago(parseDate(run.finished ?? run.started)).replacingOccurrences(of: "前", with: "")).font(.caption).foregroundStyle(.secondary) }
                        }
                    }
                }
                if (r.value?.runs ?? []).isEmpty { Text("还没有任务").foregroundStyle(.secondary) }
            } footer: { if let msg { Text(msg).foregroundStyle(.orange) } }
        }
        .refreshable { await r.load() }
        .task { await r.load() }
    }
    private func statusText(_ x: AhaRun) -> String {
        let s = runStatusText(x.status, hasProposal: x.proposalPath != nil)
        return [s, x.changeId, x.title].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: " · ")
    }
    private func stopButton(_ run: AhaRun) -> some View {
        Button(arm == run.uuid ? "确认停止" : "停止") {
            if arm == run.uuid { arm = nil; Task { await stop(run) } }
            else { arm = run.uuid; Task { try? await Task.sleep(nanoseconds: 4_000_000_000); if arm == run.uuid { arm = nil } } }
        }
        .buttonStyle(.borderedProminent).tint(arm == run.uuid ? .red : .gray).controlSize(.small)
    }
    private func stop(_ run: AhaRun) async {
        do { _ = try await PanelAPI.request("v1/claude/runs/\(run.uuid)/stop", method: "POST", timeout: 20); msg = nil }
        catch { msg = "停止失败：\(error.localizedDescription)" }
        await r.load()
    }
}

private struct RunDetail: View {
    let name: String, uuid: String
    @StateObject private var d: PanelStore<RunDetailResp>
    @State private var arm = false
    @State private var msg: String?
    init(uuid: String, name: String) {
        self.uuid = uuid; self.name = name
        _d = StateObject(wrappedValue: PanelStore<RunDetailResp>("v1/claude/runs/\(uuid)", key: "run_\(uuid)"))
    }
    var body: some View {
        DetailShell(store: d) { v in
            Section {
                LabeledContent("状态", value: runStatusText(v.status, hasProposal: v.proposal != nil))
                LabeledContent("开始", value: parseDate(v.started).map { $0.formatted(date: .abbreviated, time: .standard) } ?? "—")
                if let f = parseDate(v.finished) { LabeledContent("结束", value: f.formatted(date: .abbreviated, time: .standard)) }
                if let c = v.changeId { NavigationLink(value: PanelRoute.change(id: c, title: c)) { Text("change：\(c)").font(.callout) } }
                if v.status == "running" {
                    Button(arm ? "确认停止" : "停止") {
                        if arm { arm = false; Task { await stop() } }
                        else { arm = true; Task { try? await Task.sleep(nanoseconds: 4_000_000_000); arm = false } }
                    }.buttonStyle(.borderedProminent).tint(arm ? .red : .gray)
                    if let msg { Text(msg).font(.footnote).foregroundStyle(.orange) }
                }
            }
            Section("触发它的语音转写") {
                Text(v.transcript.isEmpty ? "（没有转写）" : v.transcript).font(.callout).textSelection(.enabled)
            }
            if let p = v.proposal { Section("Proposal") { MarkdownText(md: p) } }
            Section("运行日志（最后 40 行）") {
                if v.logTail.isEmpty { Text("没有日志").foregroundStyle(.secondary) }
                else {
                    ScrollView(.horizontal) {
                        Text(v.logTail.joined(separator: "\n")).font(.caption2.monospaced()).textSelection(.enabled)
                    }
                }
            }
        }
        .navigationTitle(name).navigationBarTitleDisplayMode(.inline)
    }
    private func stop() async {
        do { _ = try await PanelAPI.request("v1/claude/runs/\(uuid)/stop", method: "POST", timeout: 20); msg = nil }
        catch { msg = "停止失败：\(error.localizedDescription)" }
        await d.load()
    }
}

// MARK: - Digest (nightly 日报, read-only)

struct DigestResp: Codable { var kind: String; var date: String; var markdown: String; var generatedAt: String?; var availableDates: [String] }

/// Line-by-line markdown: headings/bullets styled by hand, inline (bold/links/code) via AttributedString.
private struct MarkdownText: View {
    let md: String
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(md.components(separatedBy: "\n").enumerated()), id: \.offset) { _, raw in line(raw) }
        }.frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
    }
    @ViewBuilder private func line(_ raw: String) -> some View {
        let t = raw.trimmingCharacters(in: .whitespaces)
        if t.isEmpty { Color.clear.frame(height: 2) }
        else if t.hasPrefix("#") {
            let n = t.prefix { $0 == "#" }.count
            inline(String(t.drop { $0 == "#" }).trimmingCharacters(in: .whitespaces)).font(n <= 1 ? .title3.bold() : n == 2 ? .headline : .subheadline.bold())
                .padding(.top, 6)
        } else if t.hasPrefix("- ") || t.hasPrefix("* ") {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text("•"); inline(String(t.dropFirst(2)))
            }.padding(.leading, CGFloat(raw.prefix { $0 == " " }.count) * 4)
        } else { inline(t) }
    }
    private func inline(_ s: String) -> Text {
        let opts = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return Text((try? AttributedString(markdown: s, options: opts)) ?? AttributedString(s))
    }
}

private struct DigestTab: View {
    @State private var kind = "aha"
    @State private var date: String?
    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $kind) { Text("Aha").tag("aha"); Text("Sessions").tag("sessions") }
                .pickerStyle(.segmented).padding(.horizontal).padding(.bottom, 8)
            DigestBody(kind: kind, date: $date).id(kind)
        }
        .onChange(of: kind) { _, _ in date = nil }
    }
}

private struct DigestBody: View {
    let kind: String
    @Binding var date: String?
    @StateObject private var d: PanelStore<DigestResp>
    init(kind: String, date: Binding<String?>) {
        self.kind = kind; _date = date
        _d = StateObject(wrappedValue: PanelStore<DigestResp>("v1/digest?kind=\(kind)", key: "digest_\(kind)"))
    }
    var body: some View {
        List {
            Section { StaleBanner(offline: d.offline, at: d.fetchedAt, error: d.error) }
            if let v = d.value {
                Section {
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack { ForEach(v.availableDates, id: \.self) { x in
                            Button(String(x.dropFirst(5))) { date = x; Task { await load() } }
                                .buttonStyle(.borderedProminent).tint(x == v.date ? .accentColor : .gray).controlSize(.small)
                        } }
                    }
                }
                Section { MarkdownText(md: v.markdown) } header: { Text(v.date) }
            } else if d.error == nil { Section { ProgressView() } }
            else { Section { Text("还没有日报").foregroundStyle(.secondary) } }
        }
        .refreshable { await load() }
        .task { await load() }
    }
    private func load() async {
        // Default (latest) goes through the cached store; a picked date is fetched directly.
        guard let date else { await d.load(); return }
        do {
            let data = try await PanelAPI.request("v1/digest?kind=\(kind)&date=\(date)")
            d.value = try PanelStore<DigestResp>.decode(data); d.offline = false; d.fetchedAt = Date(); d.error = nil
        } catch { d.offline = true; d.error = error.localizedDescription }
    }
}

// MARK: - Jobs (background pipeline health)

struct JobDispatch: Codable { var lastLogMtime: String?; var lastTickLine: String?; var lastTickStarted: Int? }
struct JobEnhance: Codable { var logMtime: String?; var tail: [String]?; var todayRaw: Int?; var todayEnhanced: Int?; var pending: Int? }
struct JobSynth: Codable { var state: String?; var started: String?; var finished: String?; var lastOk: String?; var error: String? }
struct JobDigest: Codable { var lastOutput: String?; var lastMtime: String? }
struct JobClaudeRun: Codable, Hashable { var uuid: String?; var name: String?; var status: String?; var started: String?; var finished: String?; var exit: Int? }
struct JobsResp: Codable {
    var serverTime: String?
    var dispatch: JobDispatch?
    var enhance: JobEnhance?
    var synthesizeTasks: JobSynth?
    var digest: JobDigest?
    var claudeRuns: [JobClaudeRun]?
}

private struct JobsTab: View {
    @StateObject private var j = PanelStore<JobsResp>("v1/jobs", key: "jobs")
    var body: some View {
        List {
            Section { StaleBanner(offline: j.offline, at: j.fetchedAt, error: j.error) }
            if let v = j.value {
                Section("Dispatch") {
                    row("最近 tick", v.dispatch?.lastLogMtime ?? "—")
                    row("本次 claude 任务数", v.dispatch?.lastTickStarted.map(String.init) ?? "—")
                    if let line = v.dispatch?.lastTickLine { Text(line).font(.caption).foregroundStyle(.secondary) }
                }
                Section("Enhance（纠错）") {
                    row("今日原文 / 已 enhance", "\(v.enhance?.todayRaw ?? 0) / \(v.enhance?.todayEnhanced ?? 0)")
                    row("pending", String(v.enhance?.pending ?? 0))
                    row("日志更新", v.enhance?.logMtime ?? "—")
                    ForEach(v.enhance?.tail ?? [], id: \.self) { Text($0).font(.caption).foregroundStyle(.secondary) }
                }
                Section("Synthesize tasks") {
                    row("state", v.synthesizeTasks?.state ?? "—")
                    row("last ok", v.synthesizeTasks?.lastOk ?? "—")
                    if let e = v.synthesizeTasks?.error, !e.isEmpty { Text(e).font(.caption).foregroundStyle(.red) }
                }
                Section("Digest") {
                    row("last output", v.digest?.lastOutput ?? "—")
                    row("mtime", v.digest?.lastMtime ?? "—")
                }
                Section("最近 Claude 运行") {
                    ForEach(v.claudeRuns ?? [], id: \.self) { r in
                        HStack {
                            Text(r.name ?? r.uuid ?? "?")
                            Spacer()
                            Text(r.status ?? "?").foregroundStyle(.secondary).font(.caption)
                        }
                    }
                }
            } else if j.error == nil { Section { ProgressView() } }
        }
        .refreshable { await j.load() }
        .task { await j.load() }
    }
    private func row(_ k: String, _ v: String) -> some View {
        HStack { Text(k); Spacer(); Text(v).foregroundStyle(.secondary).font(.caption.monospacedDigit()) }
    }
}
