# Aha Moment 日常运维

## 1. 日常使用

### 录音（一个按钮）
- 打开 App > 点大按钮：**点按开始录音** 开始录，再点（**录音中 · 点按停止**）停止，录音中显示已录时长。录一整天还是几句话由你决定，App 不区分模式；一次开始到停止 = Recordings 里一条录音 = Mac 上一个文本文件。录音时状态栏橙点常亮，可以锁屏、切 App，音频会话在后台保持（`UIBackgroundModes: audio`）。
- 每 60 秒切一个片段，进入持久队列，网络通时自动上传（每个上传请求带 `X-Aha-Session` / `X-Aha-Session-Start`，服务端据此把同一条录音的片段合并）；服务端 ACK 之前手机不删文件。
- **不要在 App 切换器里上划杀掉 App**，那会停止录音（重新打开会因为 desired=ON 继续）。
- 重启手机后需要解锁并手动打开一次 App 才会恢复录音。
- 来电 / Siri 之后可能无法在后台自动恢复，会收到 "Aha 录音已停止 — 点按恢复" 通知，点它回到 App 即继续。
- **自动上传**按钮（主界面，单个开关）：开 = Mac 可达时自动上传；关 = 只存手机、不联网（外出省电）。上传并转写完成的每一段，几秒内都会出现在 `contexts/daily_records/YYYY-MM-DD_HHMM_aha.md`，不分主动/被动。想插队：Recordings 里点那条录音左边的云朵（优先转写）。
- 已移除：按住说话（Quick）按钮及其 `source=quick` 口令分发入口；手机端不再产生 quick 片段（服务端旧的 quick 处理仍在，仅供快捷指令 `POST /v1/quick` 使用）。

### Recordings 页（回放）
主界面 **Recordings**：列表（新的在前）显示开始时间、时长、`n/m uploaded`、左侧云朵上传按钮。点进去有播放/暂停和进度条，整条录音（含还在队列里的片段）拼接播放，录制中的会显示 "Recording…" 并每 5 秒刷新。已上传的片段在手机上保留 7 天后清理。

### 看转写
```bash
ops/aha today                 # 今天所有转写，带时间
ops/aha today --date 2026-10-09
ops/aha status                # 各状态计数 + 最近 10 条
```
文件在 `~/aha_data/YYYY/MM/DD/<id>/`：`audio.m4a`、`meta.json`、`transcript.<model_slug>.txt|json`。转写按模型版本命名，多个版本并存时取最新，永不覆盖。浏览器打开 `http://localhost:8765/`（首次输入 token）也能看当天列表和回放，麦克风只在 localhost 或 HTTPS 下可用。

### Mac 上去哪里拿文字（优先转写）

优先条目（Recordings 列表里某条录音左边的**云朵上传按钮**，）转写完成后，Mac 上会同时出现：

- `~/aha_data/inbox/LATEST.txt`：最新一条文本（每次覆盖）；`ops/aha last` 直接读它/接口并打印
- `~/aha_data/inbox/priority.md`：全部优先条目，每行 `[HH:MM:SS] 文本`，最新在底部；单条在 `~/aha_data/inbox/priority/<id>.txt`
- macOS 通知「Aha 转写完成」（前 120 字；静音时显示「没有识别到语音」）
- **daily record**：`context-infrastructure/contexts/daily_records/YYYY-MM-DD_HHMM_aha.md`（一段录音一个文件，只有文字，HHMM=开始时间），转写完成几秒内自动重建，从这里复制
- 剪贴板：默认**不**自动复制（`AHA_PRIORITY_CLIPBOARD=1` 开启）；`AHA_PRIORITY_NOTIFY=0` 关通知；`AHA_DAILY_RECORD=0` 不重建 daily record
- Web UI `http://localhost:8765/`，或 `GET /v1/latest`

### iOS 快捷指令（备选）
不想用 App 时见 `ios/SHORTCUT.md`：录音 -> `POST /v1/quick?wait=1` -> 显示并复制文字，可挂到 Action Button / 轻点背面 / 锁屏小组件。

## 2. 服务启停（Mac）

两个 launchd agent：`com.aha.server`（:8765，KeepAlive）与 `com.aha.dispatch`（每 120 秒）。`com.aha.digest` 故意不加载。

```bash
launchctl print gui/$(id -u)/com.aha.server | head          # 是否已加载
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aha.server.plist     # 启动（加载）
launchctl bootout   gui/$(id -u)/com.aha.server                                  # 停止（卸载）
launchctl kickstart -k gui/$(id -u)/com.aha.server                               # 重启
# dispatch 同理，把 com.aha.server 换成 com.aha.dispatch
tail -f ~/Library/Logs/aha-server.log
tail -f ~/Library/Logs/aha-dispatch.log
```
Mac 休眠 / 合盖时服务不可达，手机会排队等 Mac 回来。改了 `asr/` 或 `server/` 代码后要 `kickstart -k` 才生效。

## 3. 状态检查

```bash
ops/aha doctor                # launchd / health / disk / queue / failed / last item / aha_asr；有 FAIL 退出码 1
ops/aha status
curl http://127.0.0.1:8765/v1/health
```
手机侧：**Queue**（左上）看 Pending / Quarantined / Last ACK；**Retry now** 重置退避立即重传。

### 在 Mac 上快速转写一个音频文件
没有 AI CLI 时，直接用 `aha transcribe`（进程内跑本地模型，不需要 server；`aha` 已软链到 `/opt/homebrew/bin/aha`，任意目录可用）：
```bash
aha transcribe ~/Downloads/memo.m4a             # 文字打到 stdout，耗时/RTF 打到 stderr
aha transcribe a.m4a b.mp3 --copy               # 多文件带 === 文件名 === 头；--copy 写入剪贴板
aha transcribe talk.mov --lang zh --json -o out.json   # 完整结果(segments/model/elapsed)并存文件
aha transcribe quiet.m4a --no-vad               # 跳过 VAD 门（确定有人声时用；纯静音会幻觉出字幕话术）
aha last -n 3                                   # 最近的 priority 转写（读 ~/aha_data/inbox/LATEST.txt、priority.md，没有则回退到今天最新转写）
```
ffmpeg 能读的 m4a/aac/mp3/wav/mov/mp4 都行。首次运行要加载模型（几秒），之后同一进程内很快；实测 8.8s 中文约 2.5-4s（RTF 0.3-0.45）。`--backend mlx_whisper|sensevoice|qwen3_asr_1.7b` 可换模型。

### 模型是否正常工作
```bash
aha check     # 端到端体检，有 FAIL 退出码 1
```
依次检查：:8765 health、launchd（server/dispatch）、ASR 后端可 import、模型缓存、并真实转写 `asr/samples/zh.m4a`（输出文字+耗时）。最后一行 `RESULT: model is working` 才算通过。
注意：若 `127.0.0.1:8765` 返回 404/非 JSON，说明有野进程（如 `python -m http.server 8765 --bind 127.0.0.1`）抢占了 localhost，`aha check` 会改用 en0 局域网 IP 探测并给出该进程 PID 的 WARN；`kill <PID>` 即可。手机走局域网 IP，不受影响。

### 重跑 / 重试
```bash
ops/aha retry <id>                          # 单条 failed / failed_permanent 重排队
ops/aha reprocess --since 2026-10-09        # 用当前 ASR 重新转写某日期之后的全部（新增 transcript 版本，不覆盖）
ops/aha reprocess --since 2026-10-09 --backend sensevoice
```
ASR 失败每次运行重试 3 次，累计 6 次后变 `failed_permanent`，需要手动 `aha retry`。`reprocess` 单独起进程，别在服务正在处理大批积压时对同一批数据跑。

## 4. 7 天重装（免费签名过期）

免费 Apple ID 签名 7 天过期，过期后 App 打不开。到期前（或打不开时）：USB 直连 Mac，**手机解锁**，运行：
```bash
cd ~/work/context-infrastructure/projects/aha_moment
ops/ios_reinstall.sh
```
覆盖安装，App 容器里的队列和录音保留；**不要先删除 App**，否则会丢未上传的片段。成功标志是脚本末尾输出 `OK: signed for 7 more days (<日期>)`。设备最多 3 个免费签名 App。

## 5. 调试手机（拉 App 的事件日志）

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
xcrun devicectl list devices                       # 取 device id
xcrun devicectl device copy from --device <id> --domain-type appDataContainer \
  --domain-identifier com.shaorui0.ahamoment \
  --source "Library/Application Support/AhaMoment/events.log" --destination ./events.log
xcrun devicectl device info files --device <id> --domain-type appDataContainer \
  --domain-identifier com.shaorui0.ahamoment      # 列容器文件（queue/ recordings/ quarantine/）
```
`events.log` 记录中断、重启、路由变化。`quarantine/<id>.reason.txt` 说明为什么某个文件被隔离。

## 6. 可调参数（环境变量，写进 plist 的 EnvironmentVariables 后重启）

`AHA_DATA_DIR`（默认 ~/aha_data）、`AHA_PORT`、`AHA_HOST`、`AHA_TOKEN`、`AHA_ASR_BACKEND`、`AHA_WHISPER_PROMPT`、`AHA_PRIORITY_NOTIFY`/`AHA_PRIORITY_CLIPBOARD`（默认 1，设 0 关闭优先转写的通知/剪贴板）、`AHA_PRIORITY_DELIVER=0`（关闭全部优先交付）,以及噪声过滤阈值 `AHA_VAD_MIN_SPEECH_S`、`AHA_VAD_MIN_SPEECH_RATIO`、`AHA_WHISPER_NSP`、`AHA_WHISPER_NSP_LOGPROB`、`AHA_WHISPER_MIN_LOGPROB`、`AHA_WHISPER_MAX_COMPRESSION`（含义见 `asr/README.md`）。

## 7. 备份

```bash
AHA_BACKUP_DEST=/Volumes/X/aha_backup ops/backup.sh          # 默认 dry run
AHA_BACKUP_DEST=/Volumes/X/aha_backup ops/backup.sh --run
```

## 8. 排障表

| 症状 | 原因 | 处理 |
|---|---|---|
| 手机 Test /v1/health 失败，Mac 本机 curl 通 | 网络不在同一网段；Backup IP 过期；macOS 防火墙拦截 python；本机有别的进程占 8765 | `ipconfig getifaddr en0` 更新 Backup；Safari 访问 `http://<ip>:8765/v1/health`；`lsof -nP -iTCP:8765 -sTCP:LISTEN` |
| `curl localhost:8765` 返回怪东西 / 手机通本机不通 | 残留的 `python -m http.server 8765` 绑在 127.0.0.1，抢了 localhost | `lsof` 找到并 kill 它，再 `kickstart -k` aha server |
| Primary 的 `.local` 一直超时 | mDNS 在这台 Mac / 网络上不解析 | 正常现象，靠 Backup（Mac IP）；每次换网络更新 IP |
| 401 | token 错 / 为空。旧版本只有点 Done 才保存 token | 重新粘贴（现在编辑即保存）；`ops/aha token` 核对 |
| 有的片段永远不上传，Queue 里不动 | 旧版本：没有 `.json` sidecar 的 `.m4a` 不会被上传 | 已修复，App 启动会"打捞"孤儿文件；重装最新版并重开 App |
| 离开家 / 出门后 Pending 一直涨 | 没有 Tailscale，手机连不上 Mac（预期行为） | 回到同网络（或让 Mac 连手机热点并更新 Backup IP）后自动补传 |
| `ops/aha ...` 报找不到 mlx / aha_asr | 没用 venv 的 Python | 现在 `ops/aha` 会自动 re-exec 到 `.venv/bin/python`；确认 `.venv` 存在、`aha_asr_dev.pth` 在 |
| 安静环境出现"谢谢观看"之类幻觉文字 | Whisper 对噪声 / 静音幻觉 | 已有三层过滤（VAD gate、段级 logprob / compression 阈值、stock phrase guard）；可调 `AHA_VAD_*`、`AHA_WHISPER_*`；然后 `ops/aha reprocess --since D` |
| 转写出繁体中文 | 无初始 prompt | 默认已带简体 prompt，`AHA_WHISPER_PROMPT` 可改 |
| `aha doctor` 里 aha_asr WARN | venv 缺依赖或 `.pth` 丢了 | 重做 SETUP_MAC.md 第 2 步 |
| 记录条目卡在 failed / failed_permanent | ASR 累计失败 | 看 `~/Library/Logs/aha-server.log`，修好后 `ops/aha retry <id>` |
| App 不能启动 / 闪退，桌面图标灰 | 7 天签名过期；或未信任开发者 | `ops/ios_reinstall.sh`；Settings > General > VPN & Device Management > Trust |
| 安装失败 `kAMDMobileImageMounterDeviceLocked` | 安装时手机锁屏 | 保持解锁重跑 |
| 录音中断后没恢复 | 来电 / Siri 后 iOS 不允许后台恢复麦克风 | 点 "Aha 录音已停止" 通知或手动打开 App；看 `events.log` |
| 手机一直 Pending，Mac 服务正常 | Local Network 权限没开 | 手机 Settings > Aha > Local Network 打开 |
