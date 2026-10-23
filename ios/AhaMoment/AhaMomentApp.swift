import SwiftUI
import UserNotifications

final class AppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    /// Show state banners even while the app is in the foreground.
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }

    /// Called when the user swipes the app away while it runs in the background (not on crash/jetsam: watchdog covers those).
    func applicationWillTerminate(_ application: UIApplication) {
        guard Settings.captureDesired else { return }
        appendEvent("app terminated while recording")
        Notify.disarmWatchdog()
        Notify.post("aha.state", "Aha 已被退出，录音已停止", "点按重新打开以继续录音", after: 1)
    }

    func application(_ application: UIApplication,
                     handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        UploadQueue.shared.bgCompletion = completionHandler
    }
}

@main
struct AhaMomentApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var queue = UploadQueue.shared
    @StateObject private var capture = CaptureEngine(queue: UploadQueue.shared)
    @Environment(\.scenePhase) private var phase

    init() {
        Dirs.prepare()
        UploadQueue.shared.recoverOrphans()   // before capture starts, so only crash leftovers are touched
        DispatchQueue.global(qos: .utility).async { RecordingStore.prune() }
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(queue)
                .environmentObject(capture)
                .onOpenURL { url in
                    if Settings.applyConfigURL(url) { queue.kick(resetBackoff: true) }
                }
        }
        .onChange(of: phase) { _, p in
            if p == .active {
                capture.resumeIfNeeded()
                queue.kick(resetBackoff: true)
            }
        }
    }
}
