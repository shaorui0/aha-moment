# Aha Moment iOS 功耗分析（持续后台录音）

> 基于 `ios/AhaMoment/*.swift` 读码（2026-10-12 快照）+ DEVELOPER.md 第 6 节。**所有 %/h 数字是估算，不是实测**；第 4 节给出实测协议，以实测为准。
> 目标设备：iPhone 16 Plus（电池约 4674 mAh ≈ 18 Wh，1% ≈ 180 mWh；100 mW 持续功耗 ≈ 0.55 %/h）。

## 0. 结论

- 功耗大头是「麦克风/音频子系统 + CPU 不能进深睡」，这是常开录音的地板，代码层只能压缩 10-30%。
- 外出只录音：约 **1.0-2.0 %/h**（含手机待机基线，约 0.3-0.5 %/h）；录一整天（16h）约 16-32%。
- 夜间在家 录音+上传（Mac 热点可达）：约 **1.3-2.7 %/h**；批量上传比逐片上传更省。
- 服务器不可达（最坏）：约 **2.5-5 %/h**，主要来自每 ~30s 一次的 health probe 与网络栈唤醒；这是最该砍的点。
- 经验参照：Apple 论坛有开发者测得「常开录音」使整机续航降 18%-50%（iPhone 6S/7 时代，非每小时数据）；Apple DTS 也确认持续录音会快速耗电。无权威公开的 iPhone 16 每小时数字，故需自测。

## 1. 后台活动清单（当前代码）

| # | 活动 | 代码位置 | 频率 | 唤醒/CPU/射频成本 | 评估 |
|---|---|---|---|---|---|
| 1 | AVAudioSession `.playAndRecord` + `.mixWithOthers`，常开 | CaptureEngine.startEngine | 常驻 | 麦克风 ADC + 音频 IO 线程常驻；系统被迫维持音频 IO 唤醒 | 地板，最大头 |
| 2 | `installTap(bufferSize: 4096)` | CaptureEngine:~L120 | 48kHz 下约 11.7 次/s（4096/48000=85ms）；bufferSize 只是建议值，实际由 IO buffer 决定 | 每次回调一次 tap 线程唤醒 | 中；唤醒率偏高 |
| 3 | 采样率转换 48k→16k（AVAudioConverter）+ 每 buffer 新分配 `AVAudioPCMBuffer` | SegmentWriter.append | 同上，约 12 次/s | SRC 约 1-2% 单核；每次堆分配 + 释放 | 中低；可复用 buffer |
| 4 | per-buffer RMS（标量 for 循环） | SegmentWriter.append | 12 次/s x ~1365 样本 | 可忽略（<0.1% 核），vDSP 更省 | 低 |
| 5 | AAC 编码 32kbps（AVAudioFile 写 ADTS，写入即编码） | `f.write(from:)` | 12 次/s | 硬件/软件 AAC 编码，约 1-3% 单核；且每次触发小 write（可能 flush） | 中 |
| 6 | 分片轮转 60s：关文件、SHA256（约 240KB）、sidecar JSON 原子写 | closeLocked/finalize（utility 队列） | 60 次/h | SHA256 毫秒级，近乎 0；文件 IO 3 次/片 | 低 |
| 7 | 水印通知 `Notify.armWatchdog()`：每片 60s 调一次 `UNUserNotificationCenter.add`（同 id 覆盖，180s 触发） | onFinished | 60 次/h | 跨进程 IPC 到 usernoted，每次约几十 ms | 低，但可降为 120s 一次 |
| 8 | UploadQueue `kick()`：每片结束扫一次队列（读目录+解码全部 sidecar） | scanLocked | 60 次/h + 路径变化 | 积压越多越贵（O(n) JSON 解码）；record-only 模式也会跑 | 低-中 |
| 9 | 健康探测 `resolveBase`：GET /v1/health，timeout 3s，**成功缓存 60s，失败不缓存**，失败后各片 `defer 30s` → 30s 后再扫再探 | resolveBase / scanLocked | 不可达时约 120 次/h x（主+备）两个 base | 每次唤醒网络栈；DNS（`.local` mDNS）+ TCP SYN 超时 3s，蜂窝/Wi-Fi 射频被拉起；最贵的非音频成本 | **高（最坏情形）** |
| 10 | 后台 URLSession（`waitsForConnectivity=true`、`isDiscretionary=false`） | UploadQueue.init | 可达时每片 1 个 PUT（约 240KB） | 非 discretionary，立即走射频；每片一次 Wi-Fi 唤醒+TCP/HTTP | 中（逐片传输的固定开销远大于 240KB 数据量） |
| 11 | `NWPathMonitor` | UploadQueue.init | 仅路径变化 | 可忽略；但每次「恢复在线」会清空 notBefore 并重扫 | 低 |
| 12 | 失败重试退避 `5*2^n` 至 900s | fail() | 上传失败时 | 可达但服务端报错时较省；不可达路径走 #9 的 30s | 低 |
| 13 | 引擎重试 2,4,8…60s，最多 10 次 | scheduleRetry | 仅失败时 | 短期 | 低 |
| 14 | 录音清理 `RecordingStore.prune()` | AhaMomentApp.init | 仅启动一次 | 一次性 | 可忽略 |
| 15 | UI：`@Published`（segmentCount、pending、lastError…）+ 前台 `TimelineView` 每秒 | ContentView | 后台时 SwiftUI 不渲染，TimelineView 暂停 | 后台仅微量主线程更新；**亮屏才是大头**（屏幕 >> 其它所有项） | 低（后台）/ 极高（亮屏） |
| 16 | events.log 追加写 | appendEvent | 事件级 | 可忽略 | 低 |
| 17 | `setPrefersNoInterruptionsFromSystemAlerts`、route/config 监听 | CaptureEngine | 事件级 | 可忽略 | 低 |

注：`skipSilence` 目前只在 finalize 时丢弃静音片，**编码与写盘已经发生**，不省电，只省上传/存储。

## 2. 估算（%/h，iPhone 16 Plus，含系统待机基线）

假设：屏幕熄灭、锁屏、手机放口袋/桌面；蜂窝待机正常；AAC 16k/32kbps；60s 分片。拆分用功率估算（mW）：

| 成分 | 功率估算 | %/h |
|---|---|---|
| 待机基线（屏灭，蜂窝/Wi-Fi 待机） | 60-90 mW | 0.3-0.5 |
| 音频 IO + 麦克风 + CPU 不进深睡（约 12 次/s 唤醒、SRC、AAC） | 100-250 mW | 0.6-1.4 |
| 分片/sidecar/水印/扫描 | 5-15 mW | 0.03-0.08 |
| **仅录音合计** | **170-350 mW** | **1.0-2.0** |
| 逐片 Wi-Fi 上传（热点可达，每片一次唤醒+PUT 约 0.5-1.5 J） | 额外 +50-150 mW 均值 | +0.3-0.8 |
| 不可达持续探测（每 30s 一次 DNS/SYN 超时 3s x1-2 个 base，蜂窝/Wi-Fi 射频保持活跃） | 额外 +150-500 mW | +0.8-3 |

| 场景 | 估算 %/h | 16 小时 | 不确定性说明 |
|---|---|---|---|
| (1) 外出，仅录音（理想：不探测） | **1.0-2.0** | 16-32% | 麦克风路径功耗因机型/路由（蓝牙耳机更高）变化大 |
| (2a) 夜间在家，录音+上传，热点可达 | **1.3-2.7** | — | 个人热点本身会让 Mac 侧耗电，手机侧影响小；Wi-Fi 固定开销主导 |
| (2b) 当前代码：外出但 auto 且服务器不可达 | **2.5-5** | 40-80% | **最坏情形**：多半是 probe 驱动；用 record-only 或探测退避可回到 (1) |

这些数字应视为区间中值 +/- 40%。已知参照：Apple 能效指南强调「避免不必要的唤醒、批量网络、少用后台定时器」；Apple DTS 称持续录音会快速耗电；开发者实测的「常开录音使续航降 18-50%」（iPhone 6S/7，较老）与上表量级相符，但不能折算成 %/h。
来源：[Apple Developer Forums: microphone recording in background](https://developer.apple.com/forums/thread/65604)、[Apple Forums: CannotStartRecording iOS13 thread](https://developer.apple.com/forums/thread/120038?page=4)、[Mozilla bug 1220378（AVAudioEngine 常驻后台耗电）](https://bugzilla.mozilla.org/show_bug.cgi?id=1220378)、Apple「Energy Efficiency Guide for iOS Apps」（通用原则，未逐字引用）。

## 3. 杠杆排序

状态：**[在做]** = 并行 agent 本轮已覆盖；**[待做]** = 后续。

| 排名 | 杠杆 | 预期收益 | 风险 | 状态 |
|---|---|---|---|---|
| 1 | **record-only 模式不探测、不上传、不起网络栈**（模式开关） | 外出场景：避免 +0.8-3 %/h 的最坏项，+scan 开销 | 低；积压文件需回家后手动/自动补传 | [在做] |
| 2 | **auto 模式下 probe 指数退避**（30s→60→…→10min，网络路径变化/前台/录音列表云朵上传时重置） | 不可达时 +0.8-3 → +0.1-0.3 %/h | 低；恢复上传最多延迟一个退避周期（用 NWPathMonitor 重置可缓解） | [在做] |
| 3 | **批量上传**：不是每片 kick，而是攒 N 片/每 10-15 分钟一批，一个 session 里并发 PUT；可达时一次射频唤醒传多片 | 可达时 +0.3-0.8 → +0.1-0.3 %/h | 低；上传延迟变长（夜间场景无所谓） | [待做]（并行 agent 做的是模式+退避，非攒批） |
| 4 | **增大 IO buffer 降低唤醒**：`setPreferredIOBufferDuration(0.1~0.2)`；tap bufferSize 配合 8192/16384；并请求 `setPreferredSampleRate(16000)`（硬件常仍 48k，但可减 SRC 成本） | 唤醒 12/s → 5/s 或更低，约 -0.1~0.3 %/h | 中：背景下系统可能忽略；增大 buffer 增加崩溃时丢失的音频（可忽略） | [待做]；SegmentWriter「廉价优化」若只含 vDSP/复用 buffer 则不含此项 |
| 5 | **record-only 用更大分片（如 300s）** | 分片相关开销 -80%（SHA/sidecar/水印/扫描），约 -0.02~0.05 %/h；水印通知间隔也要跟着拉长到 >=分片 x2 | 崩溃/被杀丢最多 5 分钟（ADTS 可打捞，orphan recover 兜底）；服务端按片转写延迟变长 | [待做]（可通过现有 `segmentSeconds` 设置，无需代码改动即可实测） |
| 6 | **vDSP RMS + 复用转换输出 buffer（不每次分配）** | CPU -0.05~0.15 %/h | 低 | [在做]（vDSP RMS）；buffer 复用[待做/视并行 agent 范围] |
| 7 | **水印改为 120-180s 一次或仅每 3 片续期** | 微小（-60 次 IPC/h），但水印是低成本高价值保护 | 误报窗口变长 | [待做] |
| 8 | **SHA256 不重算**：当前上传路径只用 sidecar 中已有 sha，仅 handleRejected 才重算；已满足 | 已无收益 | — | 已满足，无需动 |
| 9 | **skipSilence 提前到录制阶段**：静音时暂停编码/写盘（需 VAD，注意会丢环境音/断片语义） | 在安静环境（夜间）可省 AAC 编码+写盘 -0.1~0.3 %/h；但音频 IO 仍在，地板不变 | 中高：改变「分片连续」契约、服务端时间线 | [待做]，慎做 |
| 10 | **屏幕常灭、避免亮屏/前台**：UI 全后台时已无 TimelineView 开销；亮屏 1h 约 8-15%，远大于上述任何优化 | 习惯层面最大 | 无 | 使用建议：录音时锁屏，勿让 App 停在前台 |
| 11 | 降采样 16k 已是最低有用档；AAC 32kbps 编码 CPU 本就小，改为 24kbps 几乎无功耗收益 | 极小 | 转写质量下降 | 不建议 |
| 12 | 地板：麦克风管线本身无法关闭；唯一降本是「不录」（如夜间睡眠/充电时段暂停、规则触发）| 最大但改变产品语义 | 高 | 产品决策 |

Top 5（按「预期节电 x 可行性」）：1) record-only 不探测；2) probe 指数退避；3) 批量上传；4) 增大 IO buffer/降唤醒；5) 屏幕常灭 + 更大分片（record-only 用 300s）。

## 4. 实测协议（A/B 两小时）

依赖并行 agent 往 `events.log` 写的电量行（每 10 分钟：电量、充电状态、模式、热状态）。**下方解析按预期格式写，格式落地后请核对正则**。示例预期行：
`2026-10-12T22:10:00+09:00 battery: event=periodic level=87% state=unplugged mode=record thermal=nominal`（`mode` 取值 `auto` / `record`）

步骤：
1. 充到 >=90%，拔线，关闭低电量模式，关闭其它 App/后台刷新，屏幕锁屏，手机放置不动（固定位置、同信号环境、同一网络）。
2. A 组：模式=「仅录音」，录 2h（>=13 个采样点）。B 组：模式=「自动上传」，Mac 开热点、服务端可达，录 2h。可选 C 组：自动上传 + 关闭 Mac 服务（不可达，验证退避）。
3. 两组间隔：隔天同时段，起始电量相近（相差 <3%），避免充电/过热影响；每组重复 2 次取均值。
4. 用 `xcrun devicectl device copy from` 拉回 `Application Support/AhaMoment/events.log`（见 OPERATIONS.md）。
5. 判读：电量只有 1% 粒度（或 iOS 的 0.01 粒度，视 API），2h 线性拟合比首尾差更稳；若热状态非 nominal 则剔除该点。参考基线：另跑一组「不录音，仅屏灭待机」2h（可选）得到待机 %/h，相减得到录音增量。

分析脚本（保存到 scratchpad，如 `battery_rate.py`，用 `python3 -I battery_rate.py events.log`）：

```python
import re, sys
from datetime import datetime
import numpy as np  # 无 numpy 时改用手写最小二乘

pat = re.compile(r'^(\S+)\s+battery:?\s+.*?level=([\d.]+).*?state=(\w+).*?mode=([\w-]+)(?:.*?thermal=(\w+))?')
rows = []
for line in open(sys.argv[1], encoding='utf-8', errors='replace'):
    m = pat.match(line)
    if not m: continue
    t = datetime.fromisoformat(m.group(1))
    lvl = float(m.group(2)); lvl = lvl*100 if lvl <= 1.0 else lvl
    rows.append((t, lvl, m.group(3), m.group(4), m.group(5) or ''))

# 按连续同模式、unplugged 的区段切分
runs, cur = [], []
for r in rows:
    if r[2] != 'unplugged':
        if len(cur) > 1: runs.append(cur)
        cur = []; continue
    if cur and r[3] != cur[-1][3]:
        if len(cur) > 1: runs.append(cur)
        cur = []
    cur.append(r)
if len(cur) > 1: runs.append(cur)

for run in runs:
    hrs = np.array([(r[0]-run[0][0]).total_seconds()/3600 for r in run])
    lv = np.array([r[1] for r in run])
    if hrs[-1] < 0.5: continue
    slope = np.polyfit(hrs, lv, 1)[0]
    hot = sum(1 for r in run if r[4] not in ('', 'nominal', 'fair'))
    print(f"mode={run[0][3]:12s} {run[0][0]:%m-%d %H:%M} dur={hrs[-1]:.2f}h "
          f"{lv[0]:.0f}%->{lv[-1]:.0f}%  fit={-slope:.2f} %/h  endpoints={(lv[0]-lv[-1])/hrs[-1]:.2f} %/h  hot_samples={hot}")
```

对照：把 A/B 的 `fit` 与第 2 节区间比较；若 A（仅录音）>2.5 %/h，先查 IO buffer/热状态；若 B-A > 1 %/h，说明上传开销被低估，优先做批量上传（杠杆 3）；若 C-A > 0.5 %/h，说明退避未生效。
