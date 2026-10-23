# Aha Moment iOS 录音可靠性审计：「不静默停止」

> 需求：录音真停了，用户必须收到通知；录音还在跑，不能误报"已停止"。
> 基于 `ios/AhaMoment/*.swift` 读码（2026-10-12 快照，行号以 CaptureEngine.swift / SegmentWriter.swift / AhaMomentApp.swift 当时内容为准，其他 agent 并行改动后可能漂移）。**F1-F5 已于 2026-10-12 实现（见文末「实现状态」），下文 §1-§3 保留修复前的分析。** 未做真机验证的结论已标"推断"。

## 0. 先说结论

现有机制：即时通知（`aha.state` 被中断 / 被退出、`aha.stopped` 重启失败）+ 3 分钟 watchdog 死人开关。真正的缺口集中在 **watchdog 的"喂狗"信号选错了**：它只在"分片完成且 sidecar 写成功"时被推后（`onFinished`），而不是"确实在收到音频"。这导致两类问题同时存在：

1. **误报**：录音正常，watchdog 照样 180 秒后触发（G1-G3）。
2. **漏报 / 文案错误**：引擎活着但没数据、磁盘满，watchdog 能兜住但文案写"App 被退出或崩溃"，且 `SegmentWriter.lastError` 从未被任何地方读取（G5、G6）。

另外：中断 began 后会主动 `disarmWatchdog()`，如果 `.ended` 永远不来，则**完全静默**（G4）。

## 1. 路径总表

即时 = 事件发生时立刻发通知；watchdog = 最迟 120-180 秒（上次喂狗后 180s）；喂狗时刻 = 引擎 start 成功 / 每个分片 `onFinished`。

| # | 停止/停滞路径 | 代码行为 | 通知 | 误报可能 | 缺口 |
|---|---|---|---|---|---|
| 1 | 用户关 Ambient | `setAmbient(false)` L75-83：取消重试、disarm、发"已关闭" | 即时（用户主动，符合预期） | 无。`onFinished` 的 Task 里判了 `ambientOn == true`（L167），关闭后不会重新 arm | 无 |
| 2 | 中断开始（来电/Siri/其他 App） | `onInterruption .began` L240-251：`finish()`、停引擎、**disarmWatchdog**、发"录音被中断" | 即时 | 无（真停了）。仅打扰：短中断后马上收到"已恢复"，两条通知 | **G4**：disarm 后若 `.ended` 不来（App 被挂起、被其他音频 App 长期占用、系统不投递），不再有任何兜底 |
| 3 | 中断结束 + 重启成功 | L252-259 -> `restart("interruption ended")` 成功 -> 重新 arm + "已恢复" | 即时"已恢复" | 无 | 无 |
| 4 | 中断结束 + 重启失败（后台 `!int`） | L124：`reason == "interruption ended"` 立刻 `notifyStopped()`，随后退避重试 | 即时（`aha.stopped`） | 重试成功会 `removeDelivered` 并发"已恢复"，OK | 退避定时器在 App 被挂起后不跑（推断，session 未激活的后台 App 很快被冻结），后续靠用户点开 |
| 5 | 重试耗尽（10 次） | `scheduleRetry` L195-198：`notifyStopped()` | 即时，但**约 6 分钟后**才发 | 无 | **G7**：非"中断结束"原因的重启失败（路由变更、config change、media reset、冷启动）没有即时通知；且 App 若被冻结，这条根本不会触发，只剩 watchdog |
| 6 | 路由变化 / config change / media services reset | L264-274、L47-50、L44-46：都走 `restart`；失败则 `scheduleRetry` | 失败时：无即时，watchdog（≤3 分钟，但文案是"被退出或崩溃"） | 重启成功会重新 arm，无误报 | G7；另 `onRouteChange` 有 `running` 守卫，引擎已挂在重试期间的路由变化不会触发"立即重试" |
| 7 | 引擎启动失败（`setActive` / `e.start()` 抛错） | `restart` catch L120-126 | 同 5/6：无即时，watchdog 兜底 | 无 | G7；`ambientOn` 刚开启时 `setAmbient` L74 `if running` 才 arm，启动失败不 arm（前台用户看得见 `lastError`，可接受） |
| 8 | 麦克风权限被撤销 | 权限变更会让 iOS **杀进程**（推断，不走 `applicationWillTerminate`）-> watchdog；若进程还在，`startEngine` L143-144 `default` 分支抛 code 4 -> 无意义地重试 10 次 | watchdog 或 6 分钟后 `aha.stopped` | 无 | **G8**：权限拒绝是"不可重试"错误，应立即通知并停止重试；`undetermined -> denied` 回调（L139）只设 `lastError`，不通知 |
| 9 | 用户上划杀 App | `applicationWillTerminate` L18-23：disarm + 1 秒后发"已被退出" | 即时。实测 events.log 有 "app terminated while ambient on"（18:02:31）；通知是否到达需用户确认 | `guard Settings.captureDesired` 只看 ambient，仅 Quick 在录时被杀也会发"录音已停止"，措辞稍偏 | 被挂起（非后台录音）状态下上划不会走 `willTerminate`；Ambient 在录时 App 在后台运行，所以 OK |
| 10 | 系统杀（jetsam）/ 崩溃 | 无任何回调；靠 watchdog | watchdog，延迟 120-180 秒（`180 - 上次喂狗间隔`） | 无 | 文案准确（"被退出或崩溃"）；延迟可接受 |
| 11 | 音频 session 被停用 -> App 被挂起 | 无回调；退避定时器冻结 | 取决于 watchdog 是否仍在：路径 2 里已被 disarm，则**无** | 无 | **G4**（同 2）。路径 4 因为有即时通知所以安全 |
| 12 | 引擎"运行中但无 buffer"（静默停滞） | 无检测。`running == true`，tap 不再回调，分片不再完成 | watchdog（≤3 分钟），文案错（"App 被退出"）；且那个未关闭的分片一直开着 | 无 | **G5**：没有基于 buffer 心跳的检测，也不会自愈（本该 `restart("stall")`） |
| 13 | 磁盘满 / AVAudioFile 写失败 | `append` L164：写失败 -> 记 `lastError`、`closeLocked()`；下一个 buffer 重新 `openLocked()`，失败则 `cur == nil` 一直丢音频（L196），`running` 仍为 true | 分片不再完成 -> watchdog（≤3 分钟，文案错）；`SegmentWriter.lastError` **无人读取** | 无 | **G6**：错误被吞；用户看到"App 被退出"会误判；磁盘满时 sidecar 写失败（L226 同样只记 lastError）也不触发 `onFinished` |
| 14 | 低电量模式 | 不影响 AVAudioEngine 采集；影响后台上传/网络的积极程度 | 无需通知 | 无 | 无录音缺口；建议只在 `events.log` 记一行便于排查 |
| 15 | 设备关机 / 没电 / 重启 | 重启后 App 不会自动拉起，`captureDesired` 仍为 true，无人恢复 | 无（watchdog 作为 pending 本地通知是否跨重启保留未验证） | 无 | **G9**：无法完全解决（无 APNs）；可靠方案只有用户重新打开 App。可选：Mac 服务端 > N 分钟没收到 Ambient 分片 -> 推送（需另做） |
| 16 | 通知本身被关 / 勿扰 / 专注模式 | 所有通知静默 | 实际不可见 | - | **G10**：通知权限被拒时 App 里没有任何提示；建议 `getNotificationSettings` 检查并在 UI 红字，stopped/watchdog 用 `.timeSensitive` |

## 2. 误报专项（watchdog 在仍在录音时触发）

watchdog 的喂狗点只有两处：引擎 start 成功（L117）和 `onFinished`（L167）。凡是"在录但 180 秒内没有 `onFinished`"都会误报：

| # | 场景 | 为什么误报 | 确定性 |
|---|---|---|---|
| **G1** | `segmentSeconds > ~170`（Settings 页 Stepper 范围 10...600，ContentView.swift:140，`Settings.segmentSeconds` 只限制下限 10） | Ambient 分片长度 > 180s，第一个分片完成前 watchdog 必然触发；之后每个分片都触发一次 | 必现（配置问题） |
| **G2** | Ambient 开 + 按住 Quick 超过 ~170s（Quick 上限 `quickMaxSeconds = 600`，SegmentWriter.swift:107） | `quickBegin` 的 `switchSource` 先关掉 ambient 分片（`onFinished` 喂一次狗），之后 Quick 分片 600 秒内不会再喂 | 必现 |
| **G3** | `skipSilence` 开启且安静 > 3 分钟 | `finalize` L214-216 对静音 Ambient 分片 `removeItem` 后直接 `return`，**不调用 `onFinished`**；同样 L213（<300ms）和 L226（finalize 出错）都不喂狗 | 必现（只要用户开了"跳过静音"） |
| G3b | 默认 60s 分片下一次 finalize 变慢（SHA256 + 大积压时 utility 队列饥饿） | 理论上可 > 3 分钟 | 低概率 |
| 对照 | Quick-only（Ambient 关）长按 > 180s | `armWatchdog` 都有 `ambientOn` 守卫（L116、L167），不会触发 | 无误报（但 Quick 中途被杀也无 watchdog，属设计取舍） |

## 3. 建议修复（均未应用）

### F1. 喂狗信号改为"确实收到音频"的心跳（修 G1/G2/G3/G5，一并去掉对分片长度的耦合）

- `SegmentWriter.swift`：加 `private(set) var lastAppendAt = Date()`（用 `os_unfair_lock` 或原子 `Double`，tap 线程写）；在 `append(_:)` 入口（L144 之后，转码之前）更新。
- `CaptureEngine.swift`：新增 `private var heartbeat: Timer?`。`restart` 成功分支（L113 之后）里 `startHeartbeat()`；`teardown`（L177）里 `heartbeat?.invalidate()`；`onInterruption .began`（L240）里同样 invalidate。

  ```swift
  private func startHeartbeat() {
      heartbeat?.invalidate()
      heartbeat = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { [weak self] _ in
          MainActor.assumeIsolated {
              guard let self, self.ambientOn, self.running, let w = self.writer else { return }
              if Date().timeIntervalSince(w.lastAppendAt) < 10 { Notify.armWatchdog() }   // 只有真收到音频才喂狗
              else { self.event("stall: no buffers 10s"); self.restart(reason: "stall") }   // 静默停滞自愈
          }
      }
  }
  ```

  Ambient 录音时 App 在后台运行（audio background mode），Timer 正常触发；App 被杀/冻结时 Timer 停止，watchdog 照常触发。成本约每 30s 一次 `UNUserNotificationCenter.add`（POWER.md 第 1 节 #7 已评估为低）。
- 删掉 L167 的 `Notify.armWatchdog()`（或保留也无害，但不再是主信号）。`watchdogSeconds = 180`（L286）保持，30s 心跳留 6 倍余量。
- 效果：G1/G2/G3 误报消失；G5 在 ~10-40 秒内自愈，自愈失败进入重试，再失败 watchdog 仍兜底。

### F2. 中断 began 不要 disarm（修 G4）

- `CaptureEngine.swift:249` 删除 `Notify.disarmWatchdog()`。"录音被中断"即时通知保留；watchdog 从 began 前最后一次喂狗起算，`.ended` 重启成功会重新 arm，`.ended` 不来则 3 分钟内提醒。
- 注意：F1 的 invalidate 心跳要在 began 里做，否则心跳会在 `running == false` 时空转（上面的 guard 已覆盖，可不特别处理）。
- 同步改 `Notify.armWatchdog` 文案（L300），不要只写"App 被退出或崩溃"，改成："超过 3 分钟没有收到录音数据（被中断、App 被退出或崩溃）。点按打开以恢复。"

### F3. 引擎启动失败即时/准即时通知（修 G7）

- `restart` catch（L120-126）里：非 "interruption ended" 的失败也要通知，但避免瞬时抖动误报。方案：失败时安排一个待触发的本地通知，成功时撤销。

  ```swift
  // catch 内，ambientOn 时：
  Notify.post("aha.stopped", "Aha 录音已停止 — 点按恢复", lastError ?? "", after: 45)   // 45s 内没恢复才会弹
  // restart 成功分支（L114）里加：
  UNUserNotificationCenter.current().removePendingNotificationRequests(withIdentifiers: ["aha.stopped"])
  ```

  这样即使退避定时器因 App 被冻结而不跑，系统仍会在 45 秒后弹出，不依赖进程存活。`notifyStopped()` 内的 `stopNotified` 逻辑保留给 L124 的"立即"场景。

### F4. 麦克风权限拒绝不重试、立即通知（修 G8）

- `startEngine` L143-144：抛错前调用 `notifyStopped()`（需要把"权限"作为不可重试错误传出），`restart` catch 里识别 `(error as NSError).code == 4` 时 `return` 而不 `scheduleRetry()`；文案改"麦克风权限被关闭 — 点按去设置"。
- L139（`undetermined -> denied` 回调）同样补 `notifyStopped()`。
- 回前台 `resumeIfNeeded()` 仍会重新尝试，权限恢复后自动继续。

### F5. 暴露 SegmentWriter 错误（修 G6）

- `SegmentWriter.swift`：加 `var onError: (@Sendable (String) -> Void)?`，在 L154、L164、L196、L226 四处写 `lastError` 的地方同时调用。
- `CaptureEngine.startEngine`（L164 附近）设置 `w.onError = { [weak self] m in Task { @MainActor in self?.lastError = m; self?.event("writer error: \(m)"); self?.notifyWriterFailed(m) } }`。`notifyWriterFailed` 复用 `notifyStopped` 的样式，文案"录音写入失败：\(m)"，去重（同一错误只发一次）。
- 磁盘满预检：`openLocked()`（L181）里用 `URLResourceValues.volumeAvailableCapacityForImportantUsage` 检查，< 50 MB 时直接 `onError("存储空间不足")` 并不要继续静默丢音频。
- 写失败后（L164）不要一直每个 buffer 重试 `openLocked()`：设一个 5 秒的冷却，避免空转耗电。

### F6. 其它小项

- `onRouteChange` L266 的 `running` 守卫：当 `!running && wantRunning` 且原因是 `.newDeviceAvailable / .oldDeviceUnavailable` 时，应直接 `retryAttempt = 0; restart("route change")`，而不是等退避。
- `finalize` 三个提前 return（SegmentWriter.swift L213、L215、L226）可以调用一个新的轻量回调 `onActivity?()` 而不是 `onFinished`，但有 F1 之后不再必要。
- `applicationWillTerminate`（AhaMomentApp.swift:19）：`captureDesired` 之外，建议同时检查 Quick 在录时的措辞，或保持现状（Quick 被杀时用户本人在场，影响小）。
- 通知权限 / Time Sensitive（G10）：`requestNotificationAuth`（L214）之后启动时 `getNotificationSettings`，`authorizationStatus != .authorized` 时在 ContentView 显示红字；`aha.stopped` / `aha.watchdog` 设 `c.interruptionLevel = .timeSensitive`（需要 Time Sensitive Notifications 的 entitlement，签名后再验证）。
- 低电量模式（#14）：在 `restart` 成功时把 `ProcessInfo.processInfo.isLowPowerModeEnabled` 写进 `events.log`，仅用于事后排查。
- G9（关机/重启）：客户端无解。如需要覆盖，可在 Mac 服务端加"N 分钟没有收到 ambient 分片 -> 通知"的服务端死人开关（和 iOS 本地 watchdog 互补，服务端可见"设备整体消失"）。

## 4. 修复后的预期矩阵

| 事件 | 通知 | 延迟 |
|---|---|---|
| 中断 began | "录音被中断" + watchdog 保留 | 即时 + 3 分钟兜底 |
| 中断 ended 重启失败 | `aha.stopped` | 即时 |
| 其它重启失败（路由/config/media reset/冷启动） | 待触发 `aha.stopped` | 45 秒（成功则撤销） |
| 权限拒绝 | "麦克风权限被关闭" | 即时，且不重试 |
| 引擎静默停滞 | 先自愈，不行则上面两条 | 10-40 秒 |
| 磁盘满 / 写失败 | "录音写入失败：…" | 即时 |
| 上划杀 App | "Aha 已被退出" | 即时 |
| jetsam / 崩溃 / 进程消失 | watchdog | 120-180 秒 |
| 正在录但分片长（>180s）/ 长按 Quick / 跳过静音 | 无（修复前会误报） | - |

## 5. 验证建议

- 误报：Settings 里把 Segment 调到 300 s、Ambient 开 4 分钟不动，修复前应收到 watchdog，修复后不应；`skipSilence` 开 + 静音 4 分钟同理；Ambient 开 + 长按 Quick 4 分钟同理。
- 漏报：设备上 Ambient 开，`xcrun devicectl device process signal --device <id> --pid <pid> --signal SIGKILL`，2-3 分钟内应有 watchdog；打一通电话后挂断，观察"被中断 -> 已恢复"；用 Siri 唤醒测一次。
- 停滞：开发期可用测试开关让 tap 回调 early-return（模拟"运行但无 buffer"），应在 ~40s 内看到 `events.log` 的 `stall` 行并恢复。
- 磁盘满：用一个测试构建把 `Dirs.queue` 指到只读目录或限制空间，应立即看到写入失败通知。

## 6. 实现状态（2026-10-12，已编译通过，未真机验证）

| 缺口 | 状态 | 新行为 / 位置 |
|---|---|---|
| G1 分片 > 170 s 误报 | 已修 | 喂狗改为 30 s 心跳（`CaptureEngine.heartbeatTick`），与分片长度解耦 |
| G2 长按 Quick 误报 | 已修 | 同上，Quick 期间 buffer 仍在到达 |
| G3 skipSilence 误报 | 已修 | 同上 |
| G4 中断 began 后无兜底 | 已修 | began 不再 `disarmWatchdog()`，仅停心跳；`.ended` 不来则 watchdog ~3 分钟触发 |
| G5 静默停滞 | 已修 | `lastAppendAt` > 20 s（引擎已启动 > 20 s）-> `event("stall")` + `restart("stall")`；自愈失败走重试 / watchdog |
| G6 写错误被吞 | 已修 | `SegmentWriter.onError`（同一错误只回调一次，成功写入后清除）-> 日志 + "Aha 录音写入失败：…"（`aha.writeerr`）；失败后 5 s 冷却再开文件；启动时可用空间 < 200 MB -> `aha.disk` 警告（每小时 1 次） |
| G7 非中断类重启失败无即时通知 | 已修 | `restart` 失败（ambient 期望开）-> 待触发 `aha.stopped` +45 s，成功启动撤销 pending+delivered；"interruption ended"失败仍即时 |
| G8 麦克风权限 | 已修 | code 4 / 权限请求被拒 -> 即时"Aha 无法录音：麦克风权限被关闭（设置 > Aha > 麦克风）"，不重试；回前台 `resumeIfNeeded` 仍会再试 |
| G9 关机/重启 | 未修（客户端无解） | 可选：Mac 服务端"N 分钟无 ambient 分片"死人开关 |
| G10 通知被关 / 专注模式 | 未修 | 待做：`getNotificationSettings` 红字提示；`.timeSensitive`（需 entitlement） |
| F6 route change `running` 守卫 | 未做 | 中断期间路由变化触发重启可能与来电冲突，风险不确定，留作后续 |
| F6 低电量日志 | 已做 | `logBattery` 的 events.log 行新增 `lowpower=` 字段 |
| F6 `finalize` 提前 return 回调 | 不需要 | 心跳后无依赖 |

电量：新增 timer 为 30 s 一次（tolerance 5 s，仅 running 时存在），每次最多一次 `UNUserNotificationCenter.add`（Ambient）。

注意：`aha.stopped` 的 +45 s 待触发通知在重试期间只调度一次（`stopPending`），避免被每次退避重试推后。
