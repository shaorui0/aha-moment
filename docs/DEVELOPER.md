# Aha Moment 开发者指南

面向要理解、修改、扩展本系统的开发者（含未来的 AI agent）。运维操作见 `docs/OPERATIONS.md`，部署见 `docs/SETUP_MAC.md` / `docs/SETUP_IPHONE.md`，设计初衷见 `PLAN.md`。本文以代码为准；与 README / `ios/README.md` 冲突时信本文和代码（`ios/README.md` 里仍有 `.m4a` 的旧描述，见第 6 节与「待确认」）。

## 1. 系统概览

一句话：iPhone 只做 Capture -> Persist -> Upload，Mac 做 ASR 和一切下游。两种输入模式：

- **Ambient**：后台持续录音，每 60 秒切一个片段（`Settings.segmentSeconds`，默认 60，最小 10）。
- **Quick**：按住说话，一个按压 = 一个片段（单段上限 `SegmentWriter.quickMaxSeconds = 600` 秒），`source=quick`，转写后会进入指令分发。

```
iPhone (SwiftUI, iOS 17+, 无第三方依赖)                 Mac (launchd 常驻)
┌────────────────────────────────────┐      ┌─────────────────────────────────────────────────┐
│ CaptureEngine  (AVAudioSession/    │      │ com.aha.server  = ops/aha serve -> uvicorn :8765 │
│   AVAudioEngine tap, 状态机)       │      │  server/app.py      路由 + 鉴权 + spool + sha256 │
│      │ PCM                         │      │  server/storage.py  Store: 目录布局 + meta 索引  │
│ SegmentWriter  AAC 16k mono 32kbps │ PUT  │  server/worker.py   单线程 Worker (重试/崩溃恢复)│
│   60s 轮转 -> queue/<id>.aac       │ ───▶ │        │                                        │
│   写 sidecar <id>.json (= ready)   │ HTTP │        ▼                                        │
│      │                             │ 同网 │  asr/aha_asr  VAD gate -> backend -> guard      │
│ UploadQueue  后台 URLSession,      │      │        │                                        │
│   ACK 前不删, 退避/隔离            │      │        ▼                                        │
│      │ 200/201                     │      │  ~/aha_data/YYYY/MM/DD/<id>/                    │
│      ▼                             │      │    audio.<ext> meta.json transcript.<slug>.txt  │
│ recordings/ (7 天回放保留)         │      │  ~/aha_data/inbox/quick.jsonl  (quick 才写)      │
└────────────────────────────────────┘      │        │ com.aha.dispatch (launchd, 每 120s)     │
 备选: iOS Shortcut / Web UI                │        ▼  pipeline/dispatch_quick.py + routes.json│
   -> POST /v1/quick?wait=1                 │  inbox/todo.md notes.md answers/<id>.md unrouted.md│
                                            │  (手动) pipeline/daily_digest.py -> digest/D.md   │
                                            │  (手动) export_to_context.py -> <workspace>/     │
                                            │          contexts/daily_records/D_HHMM_aha.md  │
                                            └─────────────────────────────────────────────────┘
```

模块边界：

| 目录 | 职责 | 依赖 |
|---|---|---|
| `ios/` | 采集、切片、持久队列、回放 UI | 仅系统框架 |
| `server/` | ingest API、存储、转写 worker、内置 Web UI（`web.py` 内联 HTML） | fastapi / uvicorn / python-multipart；ASR 经 `AsrBackend` 惰性 import `aha_asr` |
| `asr/aha_asr/` | 可插拔 ASR + VAD gate + 幻觉过滤，纯函数式接口 | ffmpeg、numpy；后端各自依赖 |
| `pipeline/` | quick 分发、日摘要、导出到 context workspace；stdlib only，对 item 目录只读 | 不 import `server/` |
| `ops/` | `aha` CLI、launchd plist、`ios_reinstall.sh`、备份、慢网装包脚本 | — |
| `e2e/` | iPhone 行为模拟器 + 8 个场景 | 每场景自起 uvicorn |

## 2. 端到端数据流

### 2.1 Ambient 片段生命周期

```
App(phone)                                   Server(Mac)                      Worker / ASR
 │ setAmbient(true): Settings.captureDesired=true,
 │   新 ambientSession (持久化到 UserDefaults)
 │ AVAudioEngine tap(4096) ─ buf ─▶ SegmentWriter.append
 │   AVAudioConverter -> 16k mono float -> AVAudioFile(.aac, AAC 32kbps)
 │   frames >= 60*16000 -> closeLocked()   (下一个 buffer 开新文件, 缓冲粒度无缝)
 │ finalizeQ: 释放 AVAudioFile(flush/close) -> 计算 sha256 + 时长
 │   <300ms 删除(误触); skipSilence 且 peak<0.003 且 ambient 删除
 │   写 queue/<id>.json (atomic)  <- 这一步 = "ready"
 │ onFinished -> UploadQueue.kick()
 │ scanLocked: 取 due 项 (最多 4 并发), resolveBase 探活(primary 3s -> backup)
 │ PUT {base}/v1/audio/<id>  ───────────────▶ auth(Bearer) ; ID_RE 校验 ; X-Aha-Sha256 必填
 │   Authorization / X-Aha-Sha256 /           spool 到 .tmp/up-* 边收边算 sha256
 │   X-Aha-Recorded-At / X-Aha-Source /       sha 不等 -> 422 ; 空体 -> 400
 │   X-Aha-Duration-Ms / Content-Type         commit_upload (store.lock 内):
 │   (audio/aac)                                新 id: os.replace(tmp -> <dir>/audio.aac),
 │                                              写 meta.json -> 201 + worker.enqueue
 │                                              同 id 同 sha: 200 exists ; 异 sha: 409
 │ <──── 200/201 ───────────────────────────                                  │
 │ retain(): sidecar 标 uploaded:true,                                        ▼
 │   音频+sidecar 移到 recordings/ (保留 7 天)                       Worker._run (单线程)
 │ 409 -> quarantine/ ; 400/413/415/422 -> 重试 3 次,                   has_speech() VAD gate
 │   重算 sha 再 3 次, 仍失败 -> quarantine/                              ├ 不过 -> status=silent (不写 transcript)
 │ 其他/网络错 -> 指数退避 5s..15min                                      └ 过   -> transcribe -> add_transcript
 │                                                                         transcript.<slug>.txt/.json (永不覆盖)
 │                                                                         meta.transcripts 追加, status=done
```

要点：

- **sidecar 最后写**：`queue/` 里只有 `<id>.aac` 没有 `<id>.json` 说明进程在轮转中途死了，App 启动时 `UploadQueue.recoverOrphans()` 会打捞（见 3.5）。
- **一个目录就是队列**：`UploadQueue` 没有数据库，`scanLocked()` 每次重新列目录；重试计数 `attempts` / `notBefore` 只在内存，但"坏载荷计数"和"是否已重算 sha"存在 UserDefaults（`aha.badCounts` / `aha.rehashed`），所以"永不无限重试"跨重启成立。
- **transcript 选择**：`Store.transcript_text()` 取 `meta.transcripts` 的**最后一项**（最新的赢）。`reprocess` 追加新 slug，不覆盖旧文件。
- **手机端保留**：ACK 后不是删除而是 `retain()` 搬到 `recordings/`；`RecordingStore.prune()` 在每次 App 启动时（后台队列）删除超过 `Settings.retentionDays`（默认 7，UserDefaults 键 `recordingRetentionDays`）的文件。
- **上传基址**：`Settings.baseURLs()` = primary（默认 `http://shaoruideMacBook-Pro.local:8765`）+ backup（局域网 IP）；探测任何 HTTP 响应都算可达，结果缓存 60 秒，网络错误时作废。

### 2.2 Quick 流程

两条入口，最终都走 worker 并在完成后写 `inbox/quick.jsonl`：

```
(A) iOS App 按住说话                         (B) Shortcut / Web UI
 ContentView DragGesture                       POST /v1/quick[?wait=1]
   onChanged -> capture.quickBegin()           (raw body 或 multipart 第一个文件)
   onEnded   -> capture.quickEnd()             服务端生成 id = "q"+时间+"-"+hex4字节
 SegmentWriter.switchSource("quick")           sha 自己算, 不要求 X-Aha-Sha256, 不幂等
   closeLocked 当前 ambient 段                  commit_upload -> worker.enqueue
 quick 段 = 单文件(<=600s), 自带 quickSession   ├ wait 缺省: 201 {"status":"created","id"}
 松手 -> finalize -> sidecar -> kick            └ wait=1: 阻塞 <=60s
 走与 ambient 相同的 PUT /v1/audio/<id>             200 {status,id,text}   (done/silent, silent 时 text="")
   (X-Aha-Source: quick)                            202 {"status":"pending","text":null}  超时
                                                    502 {"status":"failed",...,"error"}
        └────────────── Worker.process ──────────────┘
                 done 且 source=="quick" -> _dispatch():
                   追加一行 JSON {id, recorded_at, text, path} 到 inbox/quick.jsonl (fsync)
                   meta.dispatched=true (幂等: 已 dispatched 不重复写)
                          │
                          ▼  com.aha.dispatch 每 120s: pipeline/dispatch_quick.py
                   cursor(inbox/.cursor, 字节偏移) + flock; 按 routes.json 首条匹配
                   待办/todo -> todo.md ; 记一下/note -> notes.md ;
                   问 claude/ask -> `claude -p --tools "" --no-session-persistence` -> answers/<id>.md ;
                   其他 -> unrouted.md
```

注意：`silent` 的 quick 不会进 `quick.jsonl`（`_dispatch` 只在 `done` 分支调用）。quick 分发由 `source=="quick"` 决定，与走哪个入口无关。

### 2.3 转写与过滤（Worker.process 内）

1. `asr.has_speech(audio)`：ffmpeg 解码 16k mono -> silero VAD（sherpa-onnx；失败退回能量法）-> `passes_gate`。不过则 `silent`，**不调用 ASR**。
2. `asr.transcribe(audio)`：再做一次 gate（`transcribe()` 内部也会 gate，空结果）-> 后端转写 -> 第 2/3 层过滤（见 7.2）。
3. `Store.add_transcript()`：slug = `slugify(result["model"] or result["backend"])`；`transcript.<slug>.txt` 或 `.json` 任一已存在就**不写**（返回 `written=False`）。这就是"换模型 = 新增文件"的实现，也是 model slug 必须带过滤版本号（`+f2`）的原因。

## 3. 不变量与契约

### 3.1 核心不变量

1. **audio 是 source of truth，transcript 是可重算的派生物。** 音频和 transcript 都不覆盖。
2. **ACK 之前手机不删文件。** 只有 200/201 才让文件离开 `queue/`（并且是搬到 `recordings/`，不是删）。
3. **转写永不覆盖**：同 slug 已存在则跳过；`reprocess` 写新 slug。
4. **静音不送 ASR**：VAD gate 在前，避免 Whisper 对静音/噪声幻觉。
5. **原子写**：服务端所有文件经 `storage.write_atomic`（同目录 `mkstemp` + `fsync` + `os.replace`）；上传先 spool 到 `<root>/.tmp/`（与数据同一文件系统）再 `os.replace`；pipeline 用 `write_atomic_text`；手机端 sidecar 用 `Data.write(options: .atomic)`。读者永远看不到半截文件。

### 3.2 PUT /v1/audio 幂等与客户端处理矩阵

| 服务端响应 | 含义 | iOS `UploadQueue` 行为 |
|---|---|---|
| 201 `created` | 首次落盘，已入 worker 队列 | `retain()` 搬 `recordings/`，清计数 |
| 200 `exists` | 同 id 同 sha256（上次 ACK 丢了） | 同 201 |
| 409 | 同 id 内容不同 | 立即隔离到 `quarantine/` + `<id>.reason.txt`，不自动重试 |
| 400 / 413 / 415 / 422 | 载荷被拒（典型：sha 不匹配） | 同一文件累计 3 次 -> 从磁盘重算 sha 回写 sidecar -> 再 3 次 -> 隔离 |
| 401 / 5xx / 网络错 / 其他 | 暂时性 | `fail()`：退避 `min(5*2^(n-1), 900)` 秒；网络恢复 (`NWPathMonitor`)、App 回前台、"Retry now" 清零退避 |

服务端对应：sha 不匹配 -> 422（临时文件删除）；缺 `X-Aha-Sha256` -> 400；id 不符 `^[A-Za-z0-9_-]{8,64}$` -> 400；`X-Aha-Source` 不在 `("ambient","quick")` -> 400；`X-Aha-Recorded-At` 非 ISO8601 -> 400；空体 -> 400。注意 401 在 iOS 里落入"其他"分支，只退避重试，不隔离。

### 3.3 Worker 状态机与重试

`transcription_status`：`pending -> processing -> done | silent | failed | failed_permanent`。

- 单次运行内最多 `MAX_ATTEMPTS = 3` 次（退避 `backoff=(2.0, 8.0)` 秒，`create_app(backoff=...)` 可注入，测试传 `()`），耗尽 -> `failed`。
- 跨重启累计 `MAX_LIFETIME_ATTEMPTS = 6`（`lifetime_attempts`），耗尽 -> `failed_permanent`。
- `Worker.start()` 崩溃恢复：对 `pending_ids()`（`pending`/`processing`/`failed`）逐个重置为 `pending, attempts=0` 并重新入队；`lifetime_attempts >= 6` 的直接标 `failed_permanent`。
- `ops/aha retry <id>` / `POST /v1/items/{id}/retry`：计数清零，重新入队。
- 并发：全局只有一个 worker 线程，ASR 串行（单模型常驻内存）。

### 3.4 鉴权

- Bearer token：`AHA_TOKEN` 环境变量 > `~/.aha/token`（`AHA_TOKEN_FILE` 可改）；首次运行用 `secrets.token_urlsafe(32)` 生成，权限 0600（`O_EXCL`）。比较用 `hmac.compare_digest`。
- `GET /v1/items/{id}/audio` 额外接受 `?t=<token>`（给 `<audio>` 标签用，它不能设 header）。
- `GET /` 和 `GET /v1/health` 无鉴权。
- iOS 端 token 存 Keychain（`kSecAttrAccessibleAfterFirstUnlock`，锁屏后台上传需要）。传输是明文 HTTP（局域网），`Info.plist` 里 `NSAllowsLocalNetworking` + `NSAllowsArbitraryLoads` 均为 true；token 在网络上不加密，这是已知取舍。

### 3.5 崩溃恢复

- **服务端**：启动时 `Store.scan()` 从 `*/meta.json` 重建内存索引；`clean_tmp()` 删除 `.tmp/` 里超过 1 小时的残留；`Worker.start()` 重新入队未完成项；quick 的 `dispatched` 标记防止重复写 inbox。`create_app()` 里 `store.items` 是内存索引，**外部进程直接改 meta.json 不会被运行中的服务看到**（所以 `aha retry` 优先走 HTTP API，服务不可达时才改磁盘，下次启动生效）。
- **手机端**：`AhaMomentApp.init()` 先 `recoverOrphans()`（此时还没开始采集，所以只会碰崩溃遗留）：`queue/` 里有 `.aac`/`.m4a` 但没有同名 `.json` 的，用 `SegmentWriter.recoverOrphan()` 试读（`AVAudioFile(forReading:)` 且 `length > 0`），成功则补写 sidecar（`source` 一律记为 `ambient`，时长由帧数推算，创建时间作 `recorded_at`），否则隔离。**为什么换 ADTS**：`.m4a` 的 moov atom 和 `.caf` 的 packet table 都在关闭时才写，进程被杀就整文件不可读；ADTS 每帧自带头，被杀后仍可读到最后一个已 flush 的帧。因此被杀后丢的最多是最后一小段。
- 手机重启后需要解锁并手动打开一次 App；`captureDesired` 持久化，打开后 `resumeIfNeeded()` 自动继续。

### 3.6 手机端 session id

一个"录音"（Recordings 页里的一条）= 一次 Ambient ON->OFF，或一次 Quick 按压。sidecar 里：

- `session_id` / `session_kind` / `session_start`：主 session（quick 段为按压 id，ambient 段为 ambient run id）。
- `ambient_session_id` / `ambient_session_start`：quick 段在 Ambient 开着时录的，同时属于该 ambient run，所以在两条录音里都出现。
- ambient session id 持久化在 UserDefaults（`ambientSessionId` / `ambientSessionStart`），中断、config change、重启恢复都沿用，直到用户关 Ambient。
- 这些字段都是 optional，旧 sidecar 仍可解码；**服务端从不看 sidecar**，只看 HTTP header。
- id 格式：`yyyyMMdd'T'HHmmss_<8位uuid小写>`（`Fmt.newSegmentID`），既是文件名也是服务端 item id。

### 3.7 模型 slug 版本化（"+f2"）

`mlx_whisper_backend.Backend.model_name = REPO + "+f2"`。"+f2" 是**过滤逻辑版本**。因为 `add_transcript` 见到同 slug 就跳过，**改了过滤/prompt/解码参数却不改 slug，`aha reprocess` 会被静默跳过（返回 `exists`）**。改了任何影响输出的逻辑，必须改 `model_name`（如 `+f3`）。slug 由 `slugify()` 把非 `[A-Za-z0-9._-]` 替换为 `_`，例如 `mlx-community_whisper-large-v3-turbo+f2` 里的 `/` 变 `_`（`+` 也会变 `_`，以代码为准）。

## 4. API 参考（server/app.py）

基址 `http://<mac>:8765`。鉴权列 "Bearer" = `Authorization: Bearer <token>`。

| 方法 路径 | 鉴权 | 请求 | 响应 |
|---|---|---|---|
| `GET /` | 无 | — | 200 内联单页 Web UI（`web.py`，`Cache-Control: no-store`；首次在页面输入 token，存 localStorage） |
| `GET /v1/health` | 无 | — | 200 `{"ok":true,"queue_depth":N}` |
| `PUT /v1/audio/{item_id}` | Bearer | 裸 body 或 multipart 首个文件；Header：`X-Aha-Sha256`（必填，小写 hex）、`X-Aha-Recorded-At`（ISO8601，缺省=now）、`X-Aha-Source`（`ambient`(默认)/`quick`）、`X-Aha-Duration-Ms`（整数，可选）、`X-Aha-Priority: high`（可选，优先转写）、`Content-Type`（决定扩展名） | 201 `{"status":"created","id"}`；200 `{"status":"exists","id"}`；400 id/头部/空体非法；401；409 同 id 异内容；422 sha 不匹配 |
| `POST /v1/quick` | Bearer | 裸 body 或 multipart；Header 同上但 sha 不要求；`?wait=1` 同步等待 | 缺省 201 `{"status":"created","id"}`；`wait=1`：200 `{"status","id","text"}`（`text` 可为 `""`）/ 202 `{"status":"pending","id","text":null}`（>60s）/ 502 `{"status":"failed","id","text":null,"error"}`；400；401 |
| `GET /v1/latest` | Bearer | — | 200 `{id,text,recorded_at,done_at}`（最近完成的优先条目）；404 尚无 |
| `GET /v1/status` | Bearer | — | 200 `{"counts":{status:n},"queue_depth","total","recent":[最近20条]}` |
| `GET /v1/items` | Bearer | `?date=YYYY-MM-DD`（缺省今天） | 200 `{"date","items":[{id,source,recorded_at,status,duration_ms,error,transcript}]}`；400 日期格式 |
| `GET /v1/items/{id}` | Bearer | — | 200 `{"meta":{...},"transcript":最新文本或null}`；404 |
| `GET /v1/items/{id}/audio` | Bearer 或 `?t=` | — | 200 `FileResponse`（`Content-Type` 按扩展名：m4a=audio/mp4, aac=audio/aac, …）；404 not found / audio missing |
| `POST /v1/items/{id}/retry` | Bearer | — | 200 `{"status":"requeued","id"}`；404 |

说明：

- 扩展名映射 `storage.EXT_BY_TYPE`：`audio/aac`、`audio/x-aac`、`audio/aacp` -> `aac`；`audio/m4a`、`audio/x-m4a`、`audio/mp4` -> `m4a`；mp3/wav/caf/ogg/webm/flac 各自映射；**未知类型默认 `m4a`**。ffmpeg 解码按内容探测，扩展名主要影响回放时的 `Content-Type`。
- 401 响应带 `WWW-Authenticate: Bearer`。
- `/v1/quick` 不校验 sha（只计算并存下），也不幂等：重复 POST 会产生多个 item。

### 优先转写（priority / "send now"）

- 触发：`PUT /v1/audio/{id}` 带 `X-Aha-Priority: high`，或 `source == quick`，或 `POST /v1/quick`（恒为优先）。meta.json 写 `priority: true`。已存在条目用同 sha 重 PUT 并带 high：幂等 200，并把 pending 条目升级（置 `priority` 并重新入队到优先级前列）。
- 调度：`Worker.q` 是 `PriorityQueue`（rank 0 优先 / 1 普通，同级 FIFO）。正在处理的普通条目不打断，完成后下一条必是优先条目。升级产生的重复队列项靠 `queued` 集合去重。
- 交付（`Worker._deliver_priority`，转写成功或判定静音后，每条只一次，由 meta `priority_done_at` 保证）：写 `~/aha_data/inbox/priority/<id>.txt`、覆盖 `inbox/LATEST.txt`、追加 `inbox/priority.md`（`[HH:MM:SS] 文本`）；`osascript` 通知（文本以 argv 传入，无注入）；`pbcopy` 写剪贴板。静音（空文本）只通知「没有识别到语音」，不写文件不碰剪贴板。通知/剪贴板失败只记日志，不影响条目状态。
- 开关：`AHA_PRIORITY_NOTIFY`（默认 1）、`AHA_PRIORITY_CLIPBOARD`（默认 0，opt-in）、`AHA_DAILY_RECORD`（默认 1：**任何**条目转写完成后，防抖 2 秒、单实例后台跑 `pipeline/sync_daily_record.sh`，重建 daily record）、`AHA_PRIORITY_DELIVER=0` 整体关闭；测试可传 `create_app(deliver=callable(item_id, text))`，conftest 已 autouse 关闭通知与剪贴板。
- `GET /v1/latest` 按 `priority_done_at` 取最新一条。

## 5. 磁盘布局

### 5.1 手机容器（`Application Support/AhaMoment/`，`Dirs.prepare()` 创建，排除 iCloud 备份，保护级别 `completeUntilFirstUserAuthentication`）

```
queue/        <id>.aac + <id>.json       待上传（目录即队列；旧版本还可能有 <id>.m4a）
recordings/   <id>.aac + <id>.json       已 ACK，sidecar 带 "uploaded": true；7 天后清理
quarantine/   <id>.aac/.json + <id>.reason.txt   服务端永不接受的文件；不删不自动重试
events.log    中断/重启/路由变化/上传错误 的追加日志（ISO 时间戳 + 文本）
```
UserDefaults：`serverURL` / `fallbackURL` / `segmentSeconds` / `skipSilence` / `captureDesired` / `recordingRetentionDays` / `ambientSessionId` / `ambientSessionStart` / `aha.badCounts` / `aha.rehashed`。Keychain：service `aha.moment`，account `token`。

sidecar（`SegmentMeta`，JSON 键）：`id, recorded_at, duration_ms, source, sha256, peak_rms, bytes, session_id, session_kind, session_start, ambient_session_id, ambient_session_start, uploaded`。

### 5.2 Mac（`AHA_DATA_DIR`，默认 `~/aha_data`，不进 git）

```
~/aha_data/
  .tmp/                          上传 spool，启动时清理 >1h 的残留
  YYYY/MM/DD/<id>/               按 recorded_at(本地时区) 分日
    audio.<ext>                  ext = aac | m4a | mp3 | wav | caf | ogg | webm | flac
    meta.json
    transcript.<slug>.txt|json   每个模型版本一对；.json 含 text/language/segments/model/backend/duration_s/elapsed_s
  inbox/
    quick.jsonl                  worker 追加；每行 {id, recorded_at, text, path}
    .cursor  .cursor.lock        dispatch 的字节偏移 + flock
    todo.md notes.md unrouted.md answers/<id>.md bad_lines.jsonl
  digest/YYYY-MM-DD.md           daily_digest.py 产物
```
`Store.scan()` 只认 `[0-9]{4}/[0-9]{2}/[0-9]{2}/*/meta.json`。

### 5.3 meta.json 字段（`Store.commit_upload` 创建）

| 字段 | 含义 |
|---|---|
| `id` | item id |
| `source` | `ambient` / `quick` |
| `recorded_at` | ISO8601（本地时区）；决定 `dir` |
| `received_at` | 服务端收到时间 |
| `sha256` / `size` | 音频内容校验 / 字节数 |
| `duration_ms` | 来自 `X-Aha-Duration-Ms`，可为 null |
| `dir` | 相对 root 的目录 `YYYY/MM/DD/<id>` |
| `audio` | 音频文件名 `audio.<ext>` |
| `transcription_status` | `pending/processing/done/silent/failed/failed_permanent` |
| `attempts` / `lifetime_attempts` | 本次运行内 / 跨重启累计尝试数 |
| `error` | 最近一次失败的 `repr(e)`，成功/silent 后置 null |
| `transcripts` | `[{slug, backend, model, created_at}]`，追加式；最后一项 = 当前生效 |
| `dispatched` | quick 是否已写入 `inbox/quick.jsonl` |

## 6. iOS 内部

源文件（`ios/AhaMoment/`）：`AhaMomentApp`（AppDelegate、scenePhase 钩子）、`CaptureEngine`（会话/引擎/状态机/通知）、`SegmentWriter`（转码、轮转、sidecar、孤儿打捞、`Dirs`/`Fmt`）、`UploadQueue`、`Recordings`（`RecordingStore`）、`RecordingsView`、`ContentView`（主界面 + Settings/Queue 页）、`Settings`（UserDefaults + Keychain + `events.log`）。

### 6.1 当前音频格式：ADTS `.aac`

`Dirs.audioExt = "aac"`：`SegmentWriter.openLocked()` 用 `AVAudioFile(forWriting: <id>.aac, settings: AAC 16 kHz mono 32 kbps)` 写分片（容器由扩展名决定）。`Dirs.audioExts = ["aac","m4a"]`，`Dirs.audio(in:id:)` 先找 `.aac` 再找 `.m4a`，所以旧版本留下的 `.m4a` 分片仍能被扫描、上传、回放、清理。上传时 `Dirs.contentType()`：`.aac` -> `audio/aac`，其余 -> `audio/m4a`；服务端据此存为 `audio.aac` / `audio.m4a`（`test_adts_aac_kept_as_aac`）。

### 6.2 CaptureEngine 状态机

`@MainActor`。单一模式：点按开始 -> 持续录音（60 秒轮转）-> 点按停止。关键状态：

- `isOn`：**期望状态**（"应该在录"），持久化到 `Settings.captureDesired`；`Settings.ambientSession`（key 名沿用旧名，保证升级时进行中的录音不丢）持久化当前录音的 `SessionRef`（id + start）。
- `running`：引擎实际在跑。
- `wantRunning = isOn`。已移除 quick（`quickBegin/quickEnd/quickSession/switchSource`）与 `sendNow`；`SegmentWriter.setSession()` 只在录音中再次点开始时换 session 标签。

```
                 setRecording(true)
      idle ───────────────────────────────────────▶ restart(reason)
        ▲                                               │
        │ setRecording(false)                           ▼
        │ -> teardown()                         startEngine() 成功? ──是──▶ running=true
        │                                               │ 否                   armWatchdog
        │                                               ▼                      "已恢复"通知(若此前停过)
        │                                        running=false, scheduleRetry
        │                                        (interruption ended 失败 -> 立刻 notifyStopped)
        │                                               │
        │               重试 2,4,8,16,32,60,60.. 秒, 最多 maxRetries=10 次
        │                                               │ 耗尽 -> notifyStopped, 等待前台
        └───────────── 任何事件(前台/路由/config/media reset/中断结束)重新 restart
```

- **启动**：`startEngine()` 先查麦克风权限（undetermined 则请求，回调后 `restart("permission granted")`）；`setCategory(.playAndRecord, mode: .default, options: [.mixWithOthers, .allowBluetooth, .defaultToSpeaker])`（`.mixWithOthers` 是为了后台可恢复，依据 Apple DTS 对 `!int` 560557684 的说法；`.defaultToSpeaker` 让 App 内回放不走听筒）；`setPrefersNoInterruptionsFromSystemAlerts(true)`（尽力而为）；`setActive(true)`；建 `AVAudioEngine`，`installTap(bufferSize: 4096)`，`SegmentWriter.append`。
- **中断**（`AVAudioSession.interruptionNotification`）：`.began` 立刻 `writer.finish()`（已录内容落盘并可上传）、停引擎、`running=false`、停心跳 timer、发"录音被中断"通知（**不取消 watchdog**：`.ended` 不来则约 3 分钟后兜底）；`.ended` 忽略 `shouldResume`（只是提示），立即 `restart("interruption ended")`（此刻系统最可能放行后台激活），失败则 `notifyStopped()` + 退避重试。
- **其他触发 restart**：路由变化（`oldDeviceUnavailable/newDeviceAvailable/override/routeConfigurationChange`，仅 `running` 时）、`AVAudioEngineConfigurationChange`（仅自己的引擎）、`mediaServicesWereReset`、每次回前台（`resumeIfNeeded()`，会清零 `retryAttempt`）。
- **`teardown(keepDesired:)`**：`restart` 内部以 `keepDesired: true` 调用，不 `setActive(false)`，因为期望仍为 ON 时不能放掉 session（后台重启的最好机会）；只有真正停止才 deactivate。
- **`finish()` 的强引用**：`SegmentWriter.closeLocked()` 里 `finalizeQ.async` 故意强捕获 self——`teardown()` 紧接着会释放 writer，弱引用会导致 sidecar 永远写不出。改这里时别改成 weak。

### 6.3 通知与 3 分钟 watchdog（死人开关）

所有通知都是本地通知，无 APNs。首次开始录音时请求通知权限。

| id | 触发 | 内容 |
|---|---|---|
| `aha.state` | 录音开/关（"Aha 录音已开启/已关闭"）、被中断、已恢复、被退出（`applicationWillTerminate`，用户上划杀掉） | 状态横幅（前台也显示，`willPresent` 返回 `[.banner,.sound]`） |
| `aha.stopped` | 期望 ON 但引擎起不来：中断结束后首次失败 / 10 次重试耗尽 -> 即时；其它原因的重启失败 -> **+45 秒的待触发通知**（`scheduleDelayedStopped`，成功启动即撤销，不依赖进程存活）；麦克风权限被拒（code 4 / 请求被拒）-> 即时"Aha 无法录音：麦克风权限被关闭…"且**不重试**。每个停止事件最多一次（`stopNotified`） | "Aha 录音已停止 — 点按恢复" |
| `aha.writeerr` | `SegmentWriter.onError`（open/write/convert/finalize 失败，同一错误只报一次；写失败后 5 秒冷却再重开） | "Aha 录音写入失败：…" |
| `aha.disk` | 引擎启动时可用空间 < 200 MB（每小时最多一次） | "Aha 存储空间不足" |
| `aha.watchdog` | 见下 | "Aha 可能已停止录音" / "超过 3 分钟没有收到音频（App 被退出、崩溃或卡住）。点按打开以恢复。" |

**watchdog**：`Notify.armWatchdog()` 以 identifier `aha.watchdog` 调度一个 180 秒后触发的本地通知；同 identifier 再次调度即替换。喂狗信号是**心跳**而非分片完成：`SegmentWriter.lastAppendAt` 在每个 tap buffer 更新；`CaptureEngine` 仅在 `running` 时跑一个 30 秒主队列 timer（`heartbeatTick`，tolerance 5 s）：`lastAppendAt` < 15 s 且 `isOn` -> `armWatchdog()`；`running` 却 > 20 s 没有 buffer（且引擎已启动 > 20 s）-> 记 `stall` 事件并 `restart("stall")` 自愈。因此分片长度（>170 s）、`skipSilence` 删静音片都不会误报。引擎启动成功与分片完成（`onFinished`）仍会顺带 arm，但不再是必需。如果 App 被崩溃/jetsam/系统杀掉/卡死，timer 停跳，3 分钟后通知触发——不依赖服务器的 dead-man switch。`disarmWatchdog()` 只在用户停止录音时调用（中断开始**不再**取消）。`applicationWillTerminate` 只在用户主动上划时被调用，崩溃/jetsam 由 watchdog 兜底。恢复成功时 `removePending/removeDeliveredNotifications(["aha.stopped"...])` 与 `removeDelivered(["aha.watchdog"])`。

### 6.4 后台模式与上传

- `UIBackgroundModes = [audio]`（`project.yml` 的 `info.properties` 和生成的 `Info.plist` 都有）：只要音频 session 活跃，App 可在后台持续录音，状态栏橙点常亮。
- 上传用后台 `URLSession`（identifier `aha.moment.upload`，`sessionSendsLaunchEvents = true`，`waitsForConnectivity`，`timeoutIntervalForResource = 600`，允许蜂窝，`maxInFlight = 4`）；`AppDelegate.handleEventsForBackgroundURLSession` 保存 completion handler，`urlSessionDidFinishEvents` 里调用。App 重启后用 `session.getAllTasks` 把 `taskDescription`（= item id）重新标为 in-flight，避免重复发起。
- 上传用 `uploadTask(with:fromFile:)`，请求头见 3.2 / 第 4 节。每个 PUT 带 `X-Aha-Session: <session_id>` 与 `X-Aha-Session-Start: <ISO8601 录音开始时间>`（取自 sidecar 的 `session_id` / `session_start`；没有 session 字段的旧分片不带这两个头），优先分片另带 `X-Aha-Priority: high`。分片一律以 `X-Aha-Source: ambient` 上传。sidecar 里旧的可选字段（`session_kind`、`ambient_session_*`）仍可解码、不再写入。

### 6.5 已知 iOS 限制

- **后台不能（重新）启动录音**：来电/Siri 打断后，iOS 往往拒绝在后台 `setActive(true)`（`!int`），所以自动恢复经常失败，靠 `aha.stopped` 通知 + 点开 App 恢复；中间有录音空档。依据与来源见 `ios/RESEARCH.md`（Apple 工程师原话："In general, it is not possible to start a recording from the background."）。
- 用户在 App 切换器里上划杀掉 App = 录音停止（`captureDesired` 仍为 true，重新打开自动继续）。
- 重启手机后，需解锁并手动打开一次 App。
- 免费 Apple ID 签名 7 天过期；最多 3 个免费签名 App。
- 同网才能上传（见第 9 节）。
- 本地网络权限必须允许，否则上传一直失败。
- 前台计时器 `TimelineView` 仅在前台 tick。

## 7. ASR 模块（asr/aha_asr）

### 7.1 接口与后端

稳定契约（`aha_asr/__init__.py`）：

```python
list_backends() -> list[str]                       # 依赖可 import 的后端
transcribe(audio_path, backend=None, language=None) -> dict
has_speech(audio_path) -> bool                     # VAD gate
```
`transcribe` 返回 `{text, language, segments:[{start,end,text}], model, backend, duration_s, elapsed_s}`；空 `text` 是合法结果。默认后端：`AHA_ASR_BACKEND` > 按 `_PREFERENCE = [mlx_whisper, sensevoice, qwen3_asr_1.7b, qwen3_asr_0.6b]` 取第一个可用。

| 注册名 | 模块 | 状态 |
|---|---|---|
| `mlx_whisper` | `backends/mlx_whisper_backend.py`，`mlx-community/whisper-large-v3-turbo`，带简体 prompt | **默认**；zh/mixed/ja/en CER 0.00-0.03（合成语音），RTF 0.14-0.20 |
| `sensevoice` | `backends/sensevoice_backend.py`，sherpa-onnx int8，按 30s 分块 | 快 5 倍但混合中英、日语差；备选 |
| `qwen3_asr_0.6b` / `qwen3_asr_1.7b` | `backends/qwen3_asr_backend.py`，需 `mlx-audio` | 代码未测试 |

后端实例在 `_instances` 里缓存（模型常驻内存）。

### 7.2 过滤三层（均可用环境变量覆盖）

| 层 | 位置 | 规则 | 环境变量（默认） |
|---|---|---|---|
| 1 VAD gate | `audio.passes_gate`（被 `has_speech` 和 `transcribe` 使用） | 语音总时长 >= 阈值 **且** 语音/总时长 >= 比例 | `AHA_VAD_MIN_SPEECH_S`(1.0) `AHA_VAD_MIN_SPEECH_RATIO`(0.08) |
| 2 段级丢弃 | `mlx_whisper_backend.keep_segment` | 丢弃：(`no_speech_prob` > NSP 且 `avg_logprob` < NSP_LOGPROB) 或 `avg_logprob` < MIN_LOGPROB 或 `compression_ratio` > MAX_COMPRESSION | `AHA_WHISPER_NSP`(0.6) `AHA_WHISPER_NSP_LOGPROB`(-0.7) `AHA_WHISPER_MIN_LOGPROB`(-1.0) `AHA_WHISPER_MAX_COMPRESSION`(2.4) |
| 3 stock phrase guard | `guard.filter_segments` | 命中 `HALLUCINATIONS`（子串）或 `HALLUCINATION_EXACT`（精确，如 "thank you"/"谢谢"）的段，仅当该段与 VAD 语音区间重叠 < 50% 时丢弃——真人在语音区间内说的"谢谢"保留 | 无（改 `guard.py` 常量） |

其他：`AHA_WHISPER_PROMPT` 覆盖初始 prompt（默认的简体中文 + 英文技术词 prompt 用来避免输出繁体）；VAD 用 silero（`silero_vad.onnx` 缓存在 `~/.cache/aha_asr/`，首次从 GitHub 下载），不可用时退回能量法（-45 dB）。mlx_whisper 的 `no_speech_prob` 是按 30s 解码窗口给的，噪声上通常为 0，所以 `compression_ratio`（循环文本）才是有效信号。

### 7.3 新增一个后端

1. 新建 `asr/aha_asr/backends/<name>_backend.py`，导出 `available() -> bool`（只做 `importlib.util.find_spec`，不要真正 import 重依赖）和 `class Backend`，带 `model_name` 属性与 `transcribe(wav_path, language) -> {"text","language","segments":[{"start","end","text"}]}`。输入保证是 16 kHz mono PCM wav（`audio.to_wav16k`）。
2. 在 `__init__.py` 的 `_REGISTRY` 加 `"<name>": "aha_asr.backends.<name>_backend"`（需要构造参数时用 `module:arg` 形式，如 `qwen3_asr_backend:0.6B`），按需要把名字加进 `_PREFERENCE`。
3. **`model_name` 要包含版本**（模型 id + 过滤版本），因为它决定 transcript 的 slug。
4. 过滤层 1 和 3 由 `__init__.transcribe()` 统一施加；层 2 目前是 whisper 后端私有的，新后端若有置信度信号，在自己的 `transcribe` 里做。
5. 验证：`.venv/bin/python asr/bench.py --backends <name>`；用 `AHA_ASR_BACKEND=<name>` 起服务，或 `ops/aha reprocess --since D --backend <name>`。

### 7.4 Bench

```bash
.venv/bin/python asr/bench.py [--backends mlx_whisper,sensevoice] [--out asr/BENCH.md]
```
每个后端在独立子进程里跑（隔离峰值 RSS 与首次加载时间），遍历 `asr/samples/*.{m4a,wav}`，与同名 `.ref.txt` 算 CER（去标点/空格/大小写后的字符编辑距离；静音参考为空，任何输出都计为插入）、RTF、RSS，结果写 `asr/BENCH.md`（**会覆盖该文件**）。现有样本是 `say` 合成语音；加真人评测就把录音 + `.ref.txt` 放进 `asr/samples/`。吞吐实测见 `e2e/RESULTS.md` 场景 8：60x60s 积压 381 s 排完，RTF 0.106。

## 8. 开发工作流

### 8.1 环境

```bash
cd projects/aha_moment
python3 -m venv .venv                                    # Python 3.13
.venv/bin/pip install fastapi uvicorn python-multipart httpx pytest
# ASR 依赖与慢网装包法见 docs/SETUP_MAC.md 第 2 步
echo "$PWD/asr" > .venv/lib/python3.13/site-packages/aha_asr_dev.pth
```
`ops/aha` 会自动 re-exec 到 `.venv/bin/python`。`server/app.py` 用 `import storage` 这种非包式导入，所以脚本和测试都靠 `sys.path.insert(0, server/)`；不要把 `server/` 改成包而不同步改 `ops/aha`、`e2e/server_harness.py`、测试 conftest。

### 8.2 测试

```bash
.venv/bin/python -m pytest server pipeline -q       # 当前 36 passed，~3 s；pytest.ini: --import-mode=importlib
.venv/bin/python -m pytest server -q                # 仅服务端（FakeASR + TestClient + 临时数据目录）
.venv/bin/python -m pytest pipeline -q              # 仅 pipeline（stub `claude` via AHA_CLAUDE_BIN，TZ=UTC）
e2e/run_all.sh [--only 3,5] [--asr auto|fake|real]  # 8 个场景，写 e2e/RESULTS.md；real ASR 全跑约 7 分钟
.venv/bin/python asr/bench.py                       # ASR 基准
```
- 服务端测试用 `create_app(data_dir=..., token=..., asr=FakeASR, backoff=())`；新测试沿用 `conftest.py` 的 `client` / `mk` / `asr` fixture。
- e2e：每个场景自起 uvicorn（临时数据目录、临时 token、空闲端口），**不碰 `~/aha_data` 和 launchd 里的服务**；`e2e/phone_sim.py` 镜像 `UploadQueue.swift` 的行为（目录即队列、sidecar、200/201 才删、409 隔离、最多 4 并发）。改了 iOS 上传语义要同步改它。注意它的文档字符串仍写 `.m4a`，语料也是 `.m4a`（服务端对两者都支持）。`e2e/.work/` 在 `.gitignore` 里。
- 场景：1 正常 10 段、2 丢 ACK 重传、3 服务端被 kill -9、4 sha 损坏、5 处理中崩溃（fake/real）、6 quick wait=1、7 静音、8 吞吐。

### 8.3 构建 / 安装 iOS

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer   # 没有 xcode-select -s，必须带
ops/ios_reinstall.sh        # 手机 USB 直连 + 解锁；xcodegen generate -> xcodebuild -> devicectl install -> launch
```
脚本找 USB 上第一个 iPhone，`xcodegen generate`，`xcodebuild ... -allowProvisioningUpdates -allowProvisioningDeviceRegistration`（DerivedData 在 `~/Library/Caches/aha_ios_build`），`devicectl device install app`，`process launch --terminate-existing`，成功输出 `OK: signed for 7 more days (<日期>)`。覆盖安装保留容器数据，**不要先卸载 App**（会丢未上传片段）。`ios/AhaMoment.xcodeproj` 是生成物（`.gitignore`），改 `ios/project.yml` 后重新 `xcodegen generate`；但 `ios/AhaMoment/Info.plist` 被生成并在仓库里，改 `info.properties` 要和它保持一致。`project.yml` 的 `DEVELOPMENT_TEAM` / `PRODUCT_BUNDLE_IDENTIFIER=com.shaorui0.ahamoment` 与 `ops/ios_reinstall.sh` 的 `BUNDLE=` 必须同步。仓库里没有 Swift 单元测试；iOS 行为靠真机清单（`ios/README.md` 的 M3 test checklist）和 `events.log` 验证。

### 8.4 读手机日志

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcrun devicectl list devices
xcrun devicectl device copy from --device <id> --domain-type appDataContainer \
  --domain-identifier com.shaorui0.ahamoment \
  --source "Library/Application Support/AhaMoment/events.log" --destination ./events.log
xcrun devicectl device info files --device <id> --domain-type appDataContainer \
  --domain-identifier com.shaorui0.ahamoment      # 列 queue/ recordings/ quarantine/
```
`events.log` 里看 `interruption began/ended (shouldResume=…, appState=…)`、`start failed (reason, appState=…)`、`retries exhausted`、`posted 'recording stopped' notification`、`upload: …`、`app terminated while ambient on`。`appState`：0=active、1=inactive、2=background。服务端日志 `~/Library/Logs/aha-server.log`。

### 8.5 安全地改东西

- **改 ASR 过滤/prompt/解码/换模型**：必须改后端 `model_name`（如 `+f2` -> `+f3`），否则 `reprocess` 会因"同 slug 已存在"跳过；改完 `kickstart -k` 服务，再 `ops/aha reprocess --since D`。旧 transcript 不会被删，新 slug 的成为"最新"。
- **改 sidecar（`SegmentMeta`）**：新字段必须 optional（`String?`/`Bool?`），旧 sidecar 要能解码，否则 `scanLocked()` 里 `try? decode` 失败会让那条**静默地不被上传**。不要重命名既有 CodingKey。服务端不读 sidecar，所以加字段不影响协议；若要让服务端知道，加 `X-Aha-*` header 并在 `parse_common` 里处理（缺省必须有默认值，旧客户端仍在用）。
- **改音频格式/扩展名**：同步三处——iOS `Dirs.audioExt/audioExts/contentType`、服务端 `EXT_BY_TYPE` 与 `MEDIA_BY_EXT`、`e2e/phone_sim.py`；保留对旧扩展名的读取（`Dirs.audio(in:id:)` 的回退查找）。
- **改 API**：保持向后兼容，因为手机上可能还有旧版 App 和未上传的旧片段；`PUT /v1/audio` 的幂等语义（201/200/409/422）是客户端状态机的前提。
- **改 ID 规则**：服务端 `ID_RE` 是 `^[A-Za-z0-9_-]{8,64}$`，iOS id 是 `yyyyMMdd'T'HHmmss_xxxxxxxx`（24 字符），quick 服务端 id 以 `q` 开头。
- **改 `routes.json`**：首条匹配生效；改完不用重启（dispatch 是定时进程，每次重新读取）。
- **服务端代码改动要 `launchctl kickstart -k gui/$(id -u)/com.aha.server` 才生效**（`ops/aha doctor` 可验证）。
- **仓库约定**：`projects/aha_moment` 是独立 git 仓库（嵌在 context-infrastructure 工作区里）；不提交数据（`data/`、`~/aha_data`）、`.venv/`、`ios/AhaMoment.xcodeproj/`、`e2e/.work/`；`PLAN.md` 视为设计记录，保持原样；plist 里写死了用户路径（`/Users/rshao/...`），迁移机器要改。不要加载 `com.aha.digest.plist`（带 `--llm`，会把转写发给 Claude API）。服务端 `claude -p` 一律 `--tools ""`，语音内容视为不可信输入，勿放宽。

## 9. 已知限制与路线图

已知限制（均已在代码/文档中体现）：

1. **无 Tailscale / 离网上传**：v1 只走局域网。手机必须与 Mac 同网（家里 Wi-Fi，或 Mac 连手机热点）才上传；离网时片段在 `queue/` 排队，回网自动补传。`.local`（mDNS）在这台机器/网络上不可解析，Backup 必须填局域网 IP，换网络要手改。
2. **7 天签名**：免费 Apple ID，过期后需 USB + `ops/ios_reinstall.sh`；$99/年 开发者计划可解除。
3. **无 APNs / 无服务器推送**：所有提醒都是本地通知；App 被杀只能靠 3 分钟 watchdog 事后提醒，无法从 Mac 侧主动唤醒或在断流时告警。
4. **后台重启限制**：来电/Siri 后 iOS 常拒绝后台恢复麦克风（`!int`），出现录音空档，需点通知/开 App。
5. 明文 HTTP + 局域网 Bearer token。
6. 真人语音 ASR 评测尚未做（bench 为合成语音）；Qwen3 后端未测试。
7. 磁盘约 14 MB/h 音频；Mac 无自动清理，靠 `ops/backup.sh`（默认 dry run）+ 手动管理。

路线图 / 想法（均未实现）：

- **云中继**：手机 -> 公网小型中继（对象存储或轻量 HTTPS 服务，保留 sha256 幂等契约）-> Mac 主动拉取，绕开"必须同网"和 VPN 限制；契约上 `PUT /v1/audio/{id}` 已经是幂等、可重放的，中继只需保证同样的 ACK 语义。
- **APNs / 开发者账号**：解除 7 天签名，同时才有可能做服务器侧断流告警与静默推送唤醒。
- `AudioRecordingIntent`（iOS 18+，见 `ios/RESEARCH.md`，可用性未验证）改善后台恢复。
- 服务端在检测到某设备长时间无新片段时告警（需要设备级心跳或 session 概念上送）。
- 真人语音语料 + bench；Qwen3 后端验证。
- 对 ASR 输出做 digest/context 导出的自动化调度（目前 `com.aha.digest` 默认不加载，`export_to_context.py` 无调度）。

## 待确认

以下是读代码时发现但未能在运行时验证、或文档与代码不一致的点：

1. ~~ADTS 实际产出~~ **已验证（macOS）**：同参数 `AVAudioFile` 写 `.aac` 得到 ADTS；写到一半 `_exit(9)` 后，`AVAudioFile(forReading:)`、`AVURLAsset`、ffmpeg 都能完整读出；同样条件下 `.m4a`（缺 moov）和 `.caf`（缺 packet table）都不可读。真机 kill 测试待做。
2. ~~`ios/README.md` 仍写 `.m4a`~~ **已更新**。`e2e/phone_sim.py` 用 `.m4a` 作测试语料，服务端两种都收，保留不改。
3. ~~slug 具体字符串~~ **已核对**：真实文件为 `transcript.mlx-community_whisper-large-v3-turbo_f2.txt`（旧版 `transcript.mlx-community_whisper-large-v3-turbo.txt` 并存）。
4. ~~pipeline 与服务端对"哪个 transcript 生效"不一致~~ **已修复**：`pipeline/aha_common.py:_transcript_text` 改为取最新，与 `server/storage.py` 一致。
5. **`reprocess` 绕过 worker 的 `silent` 判定以外的状态**：`Worker.reprocess` 对 `silent` 条目直接跳过，对其他条目（含 `failed`/`pending`）直接调 ASR 并写 transcript，但不更新 `transcription_status`；是否预期未确认。`ops/aha reprocess` 起独立进程，不更新运行中服务的内存索引（`items` / `transcripts`），服务端需重启或等下次 `scan` 才会在 API 里看到新 transcript——代码上如此，未实测。
6. ~~launchd 实际状态~~ **已核对**：`launchctl list | grep aha` 显示 `com.aha.server`（常驻）与 `com.aha.dispatch`（定时）均已加载；`com.aha.digest` 未加载。
7. **iOS 侧 401 处理**：代码里 401 走通用 `fail()` 退避分支（不隔离），本文据此描述；未在真机验证 UI 提示。
8. **`ios/project.yml` 与 `Info.plist` 的 `CFBundleShortVersionString`** 不一致（plist 为 1.0，project.yml 的 `MARKETING_VERSION` 为 0.1），以实际构建产物为准；未确认哪个胜出。
