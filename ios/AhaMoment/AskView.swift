import SwiftUI
import WebKit

/// 问答 tab: the ask_station page on claude-box (ask a question, or run a Deep Survey that ends with a PDF in the mailbox).
/// The page itself is the UI; this view only hosts it and answers the server's HTTP basic auth with the Keychain credential.
/// Configure once with a link: aha://config?ask=https://<user>:<password>@<host>/ask/
struct AskView: View {
    @AppStorage(Settings.askURLKey) private var askURL = ""
    @StateObject private var web = AskWeb()

    var body: some View {
        NavigationStack {
            Group {
                if let url = URL(string: askURL), !askURL.isEmpty {
                    AskWebView(web: web, url: url)
                        .overlay {
                            if let e = web.error {
                                ContentUnavailableView {
                                    Label("打不开问答页面", systemImage: "wifi.exclamationmark")
                                } description: { Text(e) } actions: { Button("重试") { web.reload(url) } }
                                    .background(Color(.systemBackground))
                            }
                        }
                } else {
                    ContentUnavailableView("还没配置问答入口", systemImage: "bubble.left.and.text.bubble.right",
                                           description: Text("在 Mac 上跑 ask-station skill 里的配置命令，或扫配置二维码（aha://config?ask=…）"))
                }
            }
            .navigationTitle("问答")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    if web.canGoBack { Button { web.view.goBack() } label: { Image(systemName: "chevron.left") } }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { if let u = URL(string: askURL) { web.reload(u) } } label: { Image(systemName: "arrow.clockwise") }
                }
            }
        }
    }
}

final class AskWeb: NSObject, ObservableObject, WKNavigationDelegate, WKUIDelegate {
    let view: WKWebView
    @Published var canGoBack = false
    @Published var error: String?
    private var obs: NSKeyValueObservation?

    override init() {
        view = WKWebView(frame: .zero, configuration: WKWebViewConfiguration())
        super.init()
        view.navigationDelegate = self
        view.uiDelegate = self
        view.isOpaque = false
        view.backgroundColor = .systemBackground
        view.scrollView.contentInsetAdjustmentBehavior = .never
        obs = view.observe(\.canGoBack, options: [.new]) { [weak self] v, _ in
            DispatchQueue.main.async { self?.canGoBack = v.canGoBack }
        }
    }

    func reload(_ url: URL) {
        error = nil
        if view.url == nil || view.url?.host != url.host { view.load(URLRequest(url: url)) } else { view.reload() }
    }

    func webView(_ webView: WKWebView, didReceive challenge: URLAuthenticationChallenge,
                 completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        let m = challenge.protectionSpace.authenticationMethod
        guard m == NSURLAuthenticationMethodHTTPBasic || m == NSURLAuthenticationMethodDefault else {
            return completionHandler(.performDefaultHandling, nil)
        }
        // Only hand the password to the configured host, and only once (a second challenge means it was rejected).
        guard challenge.previousFailureCount == 0, let user = Settings.askUser, let pw = Keychain.get("askPassword"),
              challenge.protectionSpace.host == URL(string: Settings.askURL)?.host else {
            DispatchQueue.main.async { self.error = "登录被拒绝：检查配置链接里的账号密码" }
            return completionHandler(.cancelAuthenticationChallenge, nil)
        }
        completionHandler(.useCredential, URLCredential(user: user, password: pw, persistence: .forSession))
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { error = nil }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError e: Error) {
        if (e as NSError).code != NSURLErrorCancelled { error = e.localizedDescription }
    }

    /// target=_blank links (the report PDF) open in the same view; the toolbar back button returns to the chat.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if navigationAction.targetFrame == nil { webView.load(navigationAction.request) }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        error = message
        completionHandler()
    }
}

private struct AskWebView: UIViewRepresentable {
    let web: AskWeb
    let url: URL
    func makeUIView(context: Context) -> WKWebView {
        if web.view.url == nil { web.view.load(URLRequest(url: url)) }
        return web.view
    }
    func updateUIView(_ v: WKWebView, context: Context) {
        if let h = v.url?.host, h != url.host { v.load(URLRequest(url: url)) }
    }
}
