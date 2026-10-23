# Aha Moment — PLAN

> 当前流水线与状态以 README.md 的「流水线」表为准；本文件保留 10-09 的原始设计与里程碑。

> Personal Capture Infrastructure：iPhone 只做 Capture → Persist → Upload；Mac 做 ASR 与一切下游。
> Audio 是 source of truth，transcript 是可重算的 derived artifact。

## 环境事实（2026-10-09 实测）

| 项 | 值 | 影响 |
|---|---|---|
| Mac | M4 Pro / 24GB / arm64 | MLX 系 ASR 可跑，1.7B 级模型无压力 |
| 磁盘 | 剩 63GB（84% 已用） | Xcode(~40GB 解包) + 模型(~8GB) 偏紧，需清理或只装 iOS 平台 |
| Xcode | **未安装**（只有 CLT） | iOS App 构建的硬阻塞，需用户从 App Store 装 |
| ffmpeg | 有 | 音频转码 OK |
| uv / tailscale | 无 | venv 用 `python3 -m venv`；外网访问后补 Tailscale |

## 架构

```
iPhone                               Mac (server, 后续可迁云)
┌──────────────────────┐             ┌──────────────────────────────────────┐
│ AVAudioEngine tap    │             │ FastAPI :8765                        │
│  → AAC 16kHz mono    │  PUT 幂等   │  PUT /v1/audio/{id}  (sha256 校验)   │
│  → 60s 切片 .m4a     │ ──────────▶ │  GET /v1/health  GET /v1/status      │
│ 本地队列(未 ACK 不删) │  Bearer     │        │                             │
│ Ambient / Quick 两模式│             │  worker: silero VAD → ASR backend    │
└──────────────────────┘             │        │                             │
iOS Shortcut（Day-1 Quick Input）     │  ~/aha_data/YYYY/MM/DD/<id>/         │
                                     │     audio.m4a  meta.json             │
                                     │     transcript.<model>.txt / .json   │
                                     │  route: quick → inbox/ 供 agent 分发  │
                                     └──────────────────────────────────────┘
```

## 核心不变量

1. Server ACK（sha256 匹配）之前，iPhone 不删本地文件
2. 同 id 重复上传：首次 201，之后 200，内容不同 409
3. 音频与 transcript 永不覆盖；换模型 = 新增 `transcript.<model>.txt`，`aha reprocess` 可重跑历史
4. 静音段不送 ASR（Whisper 类模型对静音会幻觉出"谢谢观看"之类文本）

## Milestones

| # | 内容 | 阻塞 | 完成标准 |
|---|---|---|---|
| M0 | ASR spike：mlx-whisper turbo / SenseVoice / Qwen3-ASR 0.6B·1.7B 可插拔 + benchmark | 无 | `bench.py` 出 CER/RTF/内存表；真实语料评测待用户录 10–20 条 |
| M1 | Mac server：ingest + worker + launchd + 测试 | 无 | pytest 全绿；curl 上传 → 自动出 transcript |
| M2 | iOS Shortcut Quick Input | 无（不需要 Xcode） | 手机点一下录音 → Mac 出 transcript |
| M3 | iOS App：Ambient 后台录音 + 切片 + 上传队列 | **Xcode 安装、iPhone 开发者模式** | 锁屏录 2h 无丢片，耗电实测 |
| M4 | Context 接入：每日 digest → `contexts/daily_records/`；quick 指令路由 | M1 | cron 产出当日 transcript 汇总 |
| M5 | Soak：8h 实录 + 断网/重启/来电打断 | M3 | 0 丢片，耗电/流量/存储数据落表 |

## iOS 关键约束

- 免费 Apple ID 即可装到自己手机；代价：签名 **7 天过期**需 Xcode 重装，最多 3 个 App。$99/年 解除
- 后台录音靠 `UIBackgroundModes: audio`（正规能力），状态栏橙点常亮，不是"偷偷录"
- 风险待 M3 验证：来电/Siri 打断后，App 在后台能否重新激活录音 session
- 码率：AAC 16kHz mono 32kbps ≈ 14MB/h，10h/天 ≈ 140MB

## 网络

- 第一阶段：同一 Wi-Fi，`http://<mac-lan-ip>:8765` + Bearer token（Info.plist 开 `NSAllowsLocalNetworking`）
- 出门可用：装 Tailscale（免费），手机走 `100.x` 地址；手机离线时队列积压，回网自动补传

## 目录

```
projects/aha_moment/
  PLAN.md
  asr/        backends/ + bench.py + samples/        (M0)
  server/     app.py worker.py storage.py tests/     (M1)
  ios/        AhaMoment/ (SwiftUI) + project.yml(XcodeGen) + SHORTCUT.md  (M2/M3)
  ops/        launchd plist, aha CLI
数据：~/aha_data/  （不进 git）
```

## 2026-10-18 语音补充（待办/想法，口述）
- 语音 trigger 任务当前有问题；Aha Task 失败原因不明，怀疑权限 <!-- aha:2026-10-18_2203_aha.md#1,#15 -->
- task 完成状态：是自己打勾，还是让 Agent 去发现？打勾后同步到 Session / OpenSpec Proposal <!-- aha:2026-10-18_2203_aha.md#4-15 -->
- 权限：语音通道不开权限更安全；只对 OpenSpec（写 proposal）负责，可做只读查询；尤其远程时不能做 Kubernetes 操作（「只读包含哪些方面」待定义） <!-- aha:2026-10-18_2203_aha.md#15 -->
- 录音如何发到对应的 cloud（听写疑似 Claude/云端），怎么做成一个 loop <!-- aha:2026-10-18_2203_aha.md#18-19 -->
- 校对（enhance）环节文字送出去后回不来；本地模型 vs 远程发出再取回哪个更好；想试手机上能否跑本地 model <!-- aha:2026-10-18_2203_aha.md#20 -->
- 想要一个后台处理面板，看 Aha 背后正在执行的事（以前都是 agent 自己做，不知道去哪看），问是否需要拿出来 <!-- aha:2026-10-18_2249_aha.md#1 -->
- 想把昨天和今天积压的相关事项（skills、任务）关掉/处理掉（原话含糊） <!-- aha:2026-10-18_2249_aha.md#1 -->

## 2026-10-18 处理结果

| 项 | 做了什么 | 文件 |
|---|---|---|
| a trigger 失败 | 根因是 `~/.local/bin/claude` 悬空软链指向 2.1.302（不存在）+ server plist PATH 缺 `~/.local/bin` → `shutil.which("claude")` 选到 brew 2.1.119（不认 `--permission-prompts`）。软链已重指到 2.1.300，`claude_bin()` 优先读 `AHA_CLAUDE_BIN` 且先搜 `~/.local/bin`，plist 已加 `AHA_CLAUDE_BIN=/Users/rshao/.local/bin/claude` 和 PATH 前置 | `pipeline/aha_common.py:20`、`ops/com.aha.server.plist` |
| b task 完成状态 | `STATUSES` 加 `done`；`POST /v1/tasks/done` 从任意非 done 状态标 done；`VoiceTodoView` 加「完成」按钮；GET /v1/tasks 回 `done` 字段 | `pipeline/tasks_store.py:18`、`server/tasks.py`、`ios/AhaMoment/VoiceTodoView.swift` |
| c 语音通道只读 | ALLOW 加 `~/aha_data` 的 Read/Glob/Grep + `Bash(cat/grep/ls/head/tail/wc)`；DENY 加 `~/aha_data` 的 Write/Edit/MultiEdit/NotebookEdit；PROMPT 明说只读；原 `voice_run_guard.py` 的「禁写出 workspace」仍然有效 | `pipeline/claude_tasks.py:54-75` |
| d 录音→cloud loop | 现状：录音打 `claude` 标签 → `dispatch_quick` tick 调 `claude_tasks.scan`；或手机点 `POST /v1/tasks/{id}/to_claude`。(a) 修了之后两条通道都能用 | — |
| e 校对回不来 | ReviewView poll 上限 120s → 600s，与 server `ENHANCE_WAIT_S` 对齐 | `ios/AhaMoment/ReviewView.swift:50-57` |
| f 手机本地 model（调研结论，未实现） | WhisperKit（Swift）支持 iPhone 15+ 跑 base/small 模型；whisper.cpp 走 Metal 更慢但更灵活。隐私 + 离线是主要收益，代价是 app 包增大 200-500MB 和首次下载模型。当前瓶颈不是转写而是 enhance 的 Ollama 调用——iPhone 跑 qwen3:14b 不可行（内存 24GB 要求），要降到 7B 以下。*(unverified, 2026-10-18)* | — |
| g 后台面板 | 新 `GET /v1/jobs` 汇总 dispatch tick / enhance / synthesize_tasks / digest / 最近 claude runs（文件缺失容忍）；PanelView 加 Jobs tab 展示 | `server/jobs.py`、`server/app.py`、`ios/AhaMoment/PanelView.swift` |

**验证：** `.venv/bin/python -m pytest` 201 passed（新增 6 条 jobs+done 测试）。`curl :8765/v1/jobs` 返回 JSON（本地 TestClient 验过）。
**用户动作：** (1) iOS 改动需要重装 app（SwiftUI 代码改了 PanelView/VoiceTodoView/ReviewView）；(2) `~/Library/LaunchAgents/com.aha.server.plist` 不存在，launchctl 加载被拒，需要你用你自己的终端 `cp ops/com.aha.server.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.aha.server.plist`。
