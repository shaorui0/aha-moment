import SwiftUI

/// 校对后发送: shows the Mac-corrected text of one recording, lets the user edit it, then sends it to Claude Code.
/// Draft is kept per session id in UserDefaults (survives offline / relaunch) until a successful send.
@MainActor
final class ReviewModel: ObservableObject {
    struct Reply: Decodable {
        let status: String          // pending | ready | fallback
        let version: String?
        let text: String
        let editedText: String?
    }

    /// Where the text in the box came from. `.phone` = on-device preview (Mac copy is offered, never swapped in silently).
    enum TextSource { case none, phone, macPreview, mac }

    let sid: String
    private let chunks: [RecChunk]
    @Published var text = "" { didSet { if !loadingFromServer { touched = true; saveDraft() } } }
    @Published private(set) var status = "loading"      // loading | pending | ready | fallback | notUploaded | offline
    @Published private(set) var version = ""
    @Published private(set) var sending = false
    @Published private(set) var sendError: String?
    @Published private(set) var sent = false
    @Published private(set) var pollEnded = false
    @Published private(set) var source = TextSource.none
    @Published private(set) var phoneBusy = false           // phone is still transcribing and the Mac has nothing final yet
    @Published private(set) var macOffer: String?           // Mac text waiting for "keep phone / use Mac"
    @Published private(set) var macOfferFinal = true        // false = Mac fallback text (never LLM-corrected)
    @Published private(set) var macSaysEmpty = false
    private var touched: Bool
    private var loadingFromServer = false
    private var draftKey: String { "aha.claudeDraft.\(sid)" }
    private var choiceKey: String { "aha.macChoice.\(sid)" }     // "phone" | "mac": asked once per recording

    init(sid: String, chunks: [RecChunk]) {
        self.sid = sid
        self.chunks = chunks
        // Only treat a *non-empty* saved draft as "user has touched this". An empty-string draft
        // (left behind when the user opened Review then dismissed without typing) would otherwise
        // permanently block fetch() from overwriting with the server's text.
        //
        // CRITICAL: assigning `text` triggers didSet, which sets `touched=true` and saveDraft(). We
        // need `loadingFromServer=true` around *every* text assignment in init, including the empty
        // one — otherwise the first fetch() will refuse to replace the box with the Mac transcript.
        let d = UserDefaults.standard.string(forKey: "aha.claudeDraft.\(sid)")
        let trimmed = (d ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            UserDefaults.standard.removeObject(forKey: "aha.claudeDraft.\(sid)")
            touched = false
            loadingFromServer = true; text = ""; loadingFromServer = false
        } else {
            touched = true
            loadingFromServer = true; text = d ?? ""; loadingFromServer = false
            touched = true     // restore after loadingFromServer-wrapped assignment reset it via didSet (no-op path)
        }
        refreshPhone()      // the box is never empty when an on-device transcript already exists
    }

    var hasDraft: Bool {
        let d = (UserDefaults.standard.string(forKey: draftKey) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return !d.isEmpty
    }
    private func saveDraft() {
        // Don't persist empty / whitespace drafts; they'd block the next fetch() loading from server.
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { UserDefaults.standard.removeObject(forKey: draftKey) }
        else { UserDefaults.standard.set(text, forKey: draftKey) }
    }

    func fetch() async {
        do {
            let d = try await PanelAPI.request("v1/sessions/\(sid)/text")
            let dec = JSONDecoder(); dec.keyDecodingStrategy = .convertFromSnakeCase
            let r = try dec.decode(Reply.self, from: d)
            status = r.status; version = r.version ?? ""
            applyServer(r)
        } catch {
            let ns = error as NSError
            status = (ns.domain == "aha" && ns.code == 404) ? "notUploaded" : "offline"
        }
    }

    private func setQuietly(_ s: String, _ src: TextSource) {
        loadingFromServer = true; text = s; loadingFromServer = false; source = src
    }

    private func applyServer(_ r: Reply) {
        if let e = r.editedText, !touched { setQuietly(e, .mac); macOffer = nil; return }   // what the user sent before
        if source == .phone {                  // phone text on screen: only offer the Mac copy once it is final
            let mac = r.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard r.status == "ready" || r.status == "fallback" else { return }
            macSaysEmpty = r.status == "ready" && (mac.isEmpty || mac.contains("无实质内容") || mac.contains("无语音") || (r.version ?? "").contains("无语音"))
            if !macSaysEmpty, mac != text.trimmingCharacters(in: .whitespacesAndNewlines),
               UserDefaults.standard.string(forKey: choiceKey) == nil {
                macOffer = r.text; macOfferFinal = r.status == "ready"
            }
            return
        }
        if !touched {                          // never overwrite what the user typed / a saved draft
            setQuietly(r.text, r.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .none : (r.status == "ready" ? .mac : .macPreview))
        }
    }

    /// Phone text -> box (only while nothing better or user-typed is there); updates `phoneBusy`.
    private func refreshPhone() {
        let fresh = chunks.filter { !$0.uploaded || -$0.recordedAt.timeIntervalSinceNow < 900 }   // old uploaded ones already have a Mac text
        let final = status == "ready" || status == "fallback"
        phoneBusy = Settings.useOnDeviceAsr && !final && fresh.contains { PhoneASR.shouldTranscribe($0.meta) && !PhoneText.exists($0.meta.id) }
        let t = PhoneText.combined(chunks)
        guard !t.isEmpty, !touched, source == .none || source == .phone, t != text else { return }
        setQuietly(t, .phone)
    }

    /// Runs next to poll(): asks the phone to transcribe whatever is missing and shows it within ~1 s of being written.
    func watchPhone() async {
        guard Settings.useOnDeviceAsr else { return }
        PhoneASR.shared.ensure(chunks.filter { !$0.uploaded || -$0.recordedAt.timeIntervalSinceNow < 900 })
        let until = Date().addingTimeInterval(180)
        repeat {
            refreshPhone()
            if !phoneBusy { return }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        } while !Task.isCancelled && Date() < until
        phoneBusy = false
    }

    func keepPhone() { macOffer = nil; UserDefaults.standard.set("phone", forKey: choiceKey) }

    /// Explicit choice: counts as the user's own text (saved as draft), so the box keeps it on reopen.
    func useMac() {
        guard let m = macOffer else { return }
        macOffer = nil; UserDefaults.standard.set("mac", forKey: choiceKey)
        source = .mac; text = m
    }

    /// Auto refresh every 5 s while the corrected text is not ready, at most 10 minutes (= server ENHANCE_WAIT_S 600).
    func poll() async {
        let started = Date()
        await fetch()
        while !Task.isCancelled, ["pending", "loading", "notUploaded", "offline"].contains(status) {
            if Date().timeIntervalSince(started) > 600 { pollEnded = true; return }
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            await fetch()
        }
    }

    func send() async {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !sending else { return }
        sending = true; sendError = nil
        defer { sending = false }
        do {
            let body = try JSONSerialization.data(withJSONObject: ["text": t])
            _ = try await PanelAPI.request("v1/sessions/\(sid)/claude", method: "POST", body: body, timeout: 15)
            Settings.claudeSessions.insert(sid)     // row shows the purple "Claude" mark, same as the sparkles button
            UserDefaults.standard.removeObject(forKey: draftKey)
            UserDefaults.standard.removeObject(forKey: choiceKey)
            sent = true
        } catch {
            let ns = error as NSError
            sendError = (ns.domain == "aha" && ns.code == 404) ? "这条录音还没上传到 Mac，上传完成后再发送（草稿已保存）"
                : (ns.domain == NSURLErrorDomain ? "离线或连不上 Mac，草稿已保存在手机上，联网后点「重试」" : "发送失败：\(ns.localizedDescription)，草稿已保存")
        }
    }
}

struct ReviewView: View {
    @Environment(\.dismiss) private var dismiss
    let session: RecSession
    @StateObject private var model: ReviewModel
    @FocusState private var focused: Bool
    @ObservedObject private var asr = PhoneASRStatus.shared

    init(session: RecSession) {
        self.session = session
        _model = StateObject(wrappedValue: ReviewModel(sid: session.id, chunks: session.chunks))
    }

    private var statusLine: (String, Color) {
        let t = model.text.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasText = !t.isEmpty
        let looksEmptyByVersion = model.version.contains("无语音") || model.version.contains("无实质") || model.version.contains("已去闲聊") && !hasText
        if model.source == .phone && hasText {          // on-device preview on screen
            switch model.status {
            case "pending", "loading": return ("手机预转（Mac 校对中…）", .orange)
            case "notUploaded": return ("手机预转 · 这条录音还没上传到 Mac，上传后会自动校对", .orange)
            case "offline": return ("手机预转 · 连不上 Mac（下拉重试）" + (model.hasDraft ? " · 草稿已保存在本机" : ""), .orange)
            default:
                if model.macSaysEmpty { return ("手机预转 · Mac 没识别出有效语音，这段可能是误识别，请核对", .orange) }
                return (model.macOffer != nil ? "手机预转 · Mac 校对稿已到，请在下方选择" : "手机预转 · 可直接改", .secondary)
            }
        }
        if !hasText && model.phoneBusy {
            if asr.line.hasPrefix("不可用") { return ("手机预转写\(asr.line)；等 Mac 校对稿", .orange) }
            let why = asr.line.hasPrefix("下载") || asr.line.hasPrefix("载入") ? "（\(asr.line)）" : ""
            return ("手机转写中…\(why) Mac 校对稿稍后到", .orange)
        }
        switch model.status {
        case "pending", "loading": return (model.pollEnded ? "校对稿还没出来，下面是未校对的原文（下拉重试）" : "正在校对…（每 5 秒自动刷新）", .orange)
        case "ready":
            if looksEmptyByVersion || (!hasText) {
                return ("没识别到有效语音（录音过短、静音，或被本地模型判为无内容）。要发给 Claude 可以在下面手动输入。", .orange)
            }
            if t.contains("无实质内容") || t.contains("无语音") {
                return ("本地模型把这段判为无内容。要发可以把下面文字改掉或清空重写。", .orange)
            }
            return ("已校对 · 可直接改", .green)
        case "fallback": return ("校对超时，下面是词表替换后的原文（未经 LLM 校对）", .orange)
        case "notUploaded": return ("这条录音还没上传到 Mac，上传后会自动出现", .orange)
        default: return ("离线或连不上 Mac · 下拉重试" + (model.hasDraft ? " · 草稿已保存在本机" : ""), .red)
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 6) {
                        if model.status == "pending" || model.status == "loading" || (model.phoneBusy && !asr.line.hasPrefix("不可用")) { ProgressView().controlSize(.small) }
                        Text(statusLine.0).font(.footnote).foregroundStyle(statusLine.1)
                    }
                    if let mac = model.macOffer {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(model.macOfferFinal ? "Mac 校对稿到了，替换?" : "Mac 原文到了（未经 LLM 校对），替换?").font(.subheadline.bold())
                            Text(mac).font(.footnote).foregroundStyle(.secondary).lineLimit(8)
                            HStack(spacing: 10) {
                                Button("保留手机版") { model.keepPhone() }.buttonStyle(.bordered)
                                Button("用 Mac 校对版") { model.useMac() }.buttonStyle(.borderedProminent).tint(.green)
                            }
                            Text("手机版有改动的话，换成 Mac 版会覆盖掉。").font(.caption2).foregroundStyle(.secondary)
                        }
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.green.opacity(0.12)))
                    }
                    TextEditor(text: $model.text)
                        .focused($focused)
                        .frame(minHeight: 280)
                        .padding(6)
                        .background(RoundedRectangle(cornerRadius: 8).stroke(Color.secondary.opacity(0.4)))
                    if let e = model.sendError { Text(e).font(.footnote).foregroundStyle(.red) }
                    NavigationLink("播放录音") { PlayerView(session: session) }.font(.footnote)
                }
                .padding(.horizontal)
            }
            .refreshable { await model.fetch() }
            HStack(spacing: 12) {
                Button("取消") { dismiss() }.buttonStyle(.bordered)
                NavigationLink {
                    ConfirmSendView(model: model)
                } label: {
                    Text("下一步 · 确认发送").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).tint(.purple)
                .disabled(model.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.horizontal).padding(.bottom, 8)
        }
        .navigationTitle("第 1 步 · 校对/修改")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItemGroup(placement: .keyboard) { Spacer(); Button("完成") { focused = false } } }
        .task { await model.poll() }
        .task { await model.watchPhone() }
        .onChange(of: model.sent) { _, ok in if ok { dismiss() } }
    }
}

/// Stage 2 — final preview + explicit send. User can go back to Stage 1 (ReviewView) to edit.
struct ConfirmSendView: View {
    @Environment(\.dismiss) private var dismiss
    @ObservedObject var model: ReviewModel

    var body: some View {
        VStack(spacing: 10) {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Claude Code 收到的是下面这段文字。发出后不会撤回。")
                        .font(.footnote).foregroundStyle(.secondary)
                    Text(model.text)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
                    if let e = model.sendError { Text(e).font(.footnote).foregroundStyle(.red) }
                }
                .padding(.horizontal)
            }
            HStack(spacing: 12) {
                Button("返回修改") { dismiss() }.buttonStyle(.bordered)
                Button {
                    Task { await model.send() }
                } label: {
                    HStack {
                        if model.sending { ProgressView().controlSize(.small) }
                        Text(model.sendError != nil ? "重试" : "发给 Claude Code").frame(maxWidth: .infinity)
                    }
                }
                .buttonStyle(.borderedProminent).tint(.purple)
                .disabled(model.sending || model.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.horizontal).padding(.bottom, 8)
        }
        .navigationTitle("第 2 步 · 确认发送")
        .navigationBarTitleDisplayMode(.inline)
    }
}
