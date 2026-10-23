import Foundation
import Security
import os

let ahaLog = Logger(subsystem: "aha.moment", category: "app")

/// Small persistent log (Application Support/AhaMoment/events.log) for post-mortem; readable via devicectl.
func appendEvent(_ s: String) {
    ahaLog.info("\(s, privacy: .public)")
    let line = "\(Fmt.iso(Date())) \(s)\n"
    let url = Dirs.root.appendingPathComponent("events.log")
    if let h = try? FileHandle(forWritingTo: url) {
        h.seekToEndOfFile(); h.write(Data(line.utf8)); try? h.close()
    } else { try? line.write(to: url, atomically: true, encoding: .utf8) }
}

/// `record`: capture only, no upload attempts and no server probing (priority chunks excepted). `auto`: upload as before.
enum UploadMode: String {
    case auto, record
}

/// UserDefaults-backed settings (views use @AppStorage with the same keys) + Keychain token.
enum Settings {
    static let serverURLKey = "serverURL"
    static let fallbackURLKey = "fallbackURL"
    static let segmentSecondsKey = "segmentSeconds"
    static let skipSilenceKey = "skipSilence"
    static let captureDesiredKey = "captureDesired"
    static let retentionDaysKey = "recordingRetentionDays"
    static let ambientSessionIdKey = "ambientSessionId"
    static let ambientSessionStartKey = "ambientSessionStart"
    static let uploadModeKey = "uploadMode"
    static let profilesKey = "profiles"            // JSON [{name,url}]
    static let activeProfileKey = "activeProfile"  // name
    static let claudeSessionsKey = "aha.claudeSessions"      // recordings (session ids) the user sent to Claude Code
    static let claudeTagPendingKey = "aha.claudeTagPending"  // subset whose server-side tag POST is not ACKed yet

    /// Uploaded recordings are kept on the phone this long (override via UserDefaults key `recordingRetentionDays`).
    static let defaultRetentionDays = 7
    static var retentionDays: Int {
        let v = UserDefaults.standard.integer(forKey: retentionDaysKey)
        return v > 0 ? v : defaultRetentionDays
    }

    /// Current ambient run, persisted so engine restarts / relaunches while desired-ON keep the same recording.
    static var ambientSession: SessionRef? {
        get {
            let d = UserDefaults.standard
            guard let id = d.string(forKey: ambientSessionIdKey) else { return nil }
            return SessionRef(id: id, start: Date(timeIntervalSince1970: d.double(forKey: ambientSessionStartKey)))
        }
        set {
            let d = UserDefaults.standard
            d.set(newValue?.id, forKey: ambientSessionIdKey)
            d.set(newValue?.start.timeIntervalSince1970 ?? 0, forKey: ambientSessionStartKey)
        }
    }

    /// Bonjour name of the Mac (survives IP changes when mDNS works). No Tailscale in v1: off-LAN uploads wait in the queue.
    static let defaultServerURL = "http://shaoruideMacBook-Pro.local:8765"
    static var claudeSessions: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: claudeSessionsKey) ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: claudeSessionsKey) }
    }
    static var claudeTagPending: Set<String> {
        get { Set(UserDefaults.standard.stringArray(forKey: claudeTagPendingKey) ?? []) }
        set { UserDefaults.standard.set(Array(newValue), forKey: claudeTagPendingKey) }
    }

    struct Profile: Codable, Equatable, Identifiable {
        var name: String
        var url: String
        var id: String { name }
    }

    /// Named server addresses. First read migrates the legacy serverURL/fallbackURL slots (old keys are left untouched).
    static var profiles: [Profile] {
        get {
            let d = UserDefaults.standard
            if let raw = d.string(forKey: profilesKey), let data = raw.data(using: .utf8),
               let p = try? JSONDecoder().decode([Profile].self, from: data) { return p }
            var p = [Profile(name: "Home (Bonjour)", url: d.string(forKey: serverURLKey) ?? defaultServerURL)]
            if let fb = d.string(forKey: fallbackURLKey), !fb.trimmingCharacters(in: .whitespaces).isEmpty {
                p.append(Profile(name: "Backup", url: fb))
            }
            profiles = p
            if d.string(forKey: activeProfileKey) == nil { d.set(p[0].name, forKey: activeProfileKey) }
            appendEvent("profiles migrated: \(p.map(\.name).joined(separator: ","))")
            return p
        }
        set {
            if let data = try? JSONEncoder().encode(newValue), let raw = String(data: data, encoding: .utf8) {
                UserDefaults.standard.set(raw, forKey: profilesKey)
            }
        }
    }
    static var activeProfile: String {
        get { UserDefaults.standard.string(forKey: activeProfileKey) ?? "" }
        set { UserDefaults.standard.set(newValue, forKey: activeProfileKey) }
    }
    /// Add or replace (by name) a profile.
    static func upsertProfile(name: String, url: String) {
        var p = profiles
        if let i = p.firstIndex(where: { $0.name == name }) { p[i].url = url } else { p.append(Profile(name: name, url: url)) }
        profiles = p
    }
    static var segmentSeconds: Int {
        let v = UserDefaults.standard.integer(forKey: segmentSecondsKey)
        return v >= 10 ? v : 60
    }
    static var uploadMode: UploadMode {
        get { UploadMode(rawValue: UserDefaults.standard.string(forKey: uploadModeKey) ?? "") ?? .auto }
        set { UserDefaults.standard.set(newValue.rawValue, forKey: uploadModeKey) }
    }
    static var skipSilence: Bool { UserDefaults.standard.bool(forKey: skipSilenceKey) }
    static var captureDesired: Bool {
        get { UserDefaults.standard.bool(forKey: captureDesiredKey) }
        set { UserDefaults.standard.set(newValue, forKey: captureDesiredKey) }
    }

    /// Segments whose peak per-buffer RMS is below this are considered silent (about -50 dBFS).
    static let silenceRMS: Float = 0.003

    static var token: String {
        get { Keychain.get("token") ?? "" }
        set { Keychain.set("token", newValue) }
    }

    /// Setup link from the Mac (`aha qr` -> QR -> Camera): aha://config?server=..&backup=..&token=..
    /// or `profile=<name>&server=<url>` to add/update a named profile (and make it active).
    /// Every parameter is optional; only the ones present are applied. Returns true if anything was applied.
    @discardableResult
    static func applyConfigURL(_ url: URL) -> Bool {
        guard url.scheme == "aha", url.host == "config",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems else { return false }
        var applied: [String] = []
        let profileName = items.first { $0.name == "profile" }?.value?.trimmingCharacters(in: .whitespacesAndNewlines).nilIfEmpty
        for it in items {
            guard let v = it.value?.trimmingCharacters(in: .whitespacesAndNewlines), !v.isEmpty else { continue }
            switch it.name {
            case "server":
                if let pn = profileName { upsertProfile(name: pn, url: v); applied.append("profile \(pn)") }
                else { upsertProfile(name: "Home (Bonjour)", url: v); applied.append("server") }
            case "backup": upsertProfile(name: "Backup", url: v); applied.append("backup")
            case "token": token = v; applied.append("token")
            default: break
            }
        }
        if let pn = profileName, applied.contains("profile \(pn)") { activeProfile = pn }
        if !applied.isEmpty {
            appendEvent("config link applied: \(applied.joined(separator: ","))")
            Notify.post("aha.config", "Aha 已完成配置", "已写入：\(applied.joined(separator: "、"))")
        }
        return !applied.isEmpty
    }

    /// Parse a user-typed server address. Accepts `http://192.168.1.5:8765`, `100.101.102.103:8765`,
    /// `mac.tail1234.ts.net:8765` (scheme defaults to http), trailing slashes. Returns nil if unusable.
    static func parseBase(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "http://" + s }
        guard let u = URL(string: s), let scheme = u.scheme?.lowercased(),
              scheme == "http" || scheme == "https",
              let host = u.host, !host.isEmpty else { return nil }
        return u
    }

    /// Candidate base URLs: active profile first, then the others in list order. Duplicates removed.
    static func baseURLs() -> [URL] {
        let all = profiles
        let act = activeProfile
        let ordered = all.filter { $0.name == act } + all.filter { $0.name != act }
        var out: [URL] = []
        for p in ordered {
            if let u = parseBase(p.url), !out.contains(u) { out.append(u) }
        }
        return out
    }

    /// First candidate (kept for callers that only need one).
    static func baseURL() -> URL? { baseURLs().first }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
private extension Optional where Wrapped == String {
    var nilIfEmpty: String? { self.flatMap { $0.isEmpty ? nil : $0 } }
}

enum Keychain {
    private static let service = "aha.moment"

    private static func query(_ key: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: key]
    }

    static func get(_ key: String) -> String? {
        var q = query(key)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess,
              let d = out as? Data else { return nil }
        return String(data: d, encoding: .utf8)
    }

    static func set(_ key: String, _ value: String) {
        SecItemDelete(query(key) as CFDictionary)
        guard !value.isEmpty else { return }
        var q = query(key)
        q[kSecValueData as String] = Data(value.utf8)
        // Readable while the phone is locked (after first unlock): needed for background uploads.
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(q as CFDictionary, nil)
    }
}
