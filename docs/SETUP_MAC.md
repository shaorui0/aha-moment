# Mac 端从零部署

目标：一台新 Mac 上跑起 `com.aha.server`（:8765）和 `com.aha.dispatch`，并能把 iPhone 编译安装所需的 Xcode 装好。以下是我们实际做过的步骤，顺序即实际顺序。

前提：Apple silicon Mac（mlx 需要）。本项目在 M4 Pro / 24GB / macOS 15.6.1 上验证。磁盘建议留 60GB 以上（Xcode 解包约 40GB + 模型约 8GB）。

> 路径说明：`ops/com.aha.server.plist` 与 `pipeline/*.plist` 里写死了 `/Users/rshao/work/context-infrastructure/projects/aha_moment` 与 `/Users/rshao/...`。换用户名或换仓库位置，必须先改 plist 里的路径（见第 6 步）。

## 1. 拿代码、建 venv

```bash
cd ~/work/context-infrastructure/projects/aha_moment      # 或你 clone 的位置
python3 -m venv .venv                                      # 我们用的是 Python 3.13（wheel 缓存是 cp313）
.venv/bin/pip install fastapi uvicorn python-multipart httpx pytest
```
`ops/aha` 会自动 re-exec 到 `.venv/bin/python`，所以之后直接 `ops/aha ...` 即可。

## 2. 装 ASR 依赖（慢网友好路径）

我们这边 pip 单连接极慢，所以用并行 HTTP range 下载 wheel 到 `~/.cache/aha_wheels`，再离线安装。网络正常可以直接 `pip install mlx-whisper`，跳到第 3 步。

```bash
# 解析依赖并并行下载大 wheel，然后离线安装
.venv/bin/python ops/fastwheel.py .venv/bin/pip ~/.cache/aha_wheels \
    mlx numpy psutil scipy numba tiktoken more-itertools tqdm huggingface_hub sherpa-onnx
# 再装不带依赖的 mlx-whisper（跳过 torch）
.venv/bin/pip install --no-deps mlx-whisper
```
`asr/README.md` 里的必需清单：mlx, mlx-metal, numpy, scipy, numba, llvmlite, tiktoken, sherpa-onnx, **sherpa-onnx-core**（带 libonnxruntime），以及 psutil more-itertools tqdm huggingface_hub regex requests，`mlx-whisper --no-deps`。
单个大包下不动时可用 `ops/bigwheels.py <pkg...>` 从 PyPI JSON API 取 cp313/macOS-arm64 的 wheel URL，下载后 `pip install --no-index --no-deps <whl>`。

让 `aha_asr` 可被导入（我们是用 `.pth` 指到 `asr/`）：
```bash
echo "$PWD/asr" > .venv/lib/python3.13/site-packages/aha_asr_dev.pth
.venv/bin/python -c "import aha_asr; print(aha_asr.list_backends())"   # 应列出 mlx_whisper
```

## 3. 模型

- Whisper：`mlx-community/whisper-large-v3-turbo`（默认，代码里写死在 `asr/aha_asr/backends/mlx_whisper_backend.py`）。首次转写会自动从 HuggingFace 下载到 `~/.cache/huggingface/hub`；慢网下用并行 range 下载器直接写进同一个 HF cache：`python3 ops/hfpar.py mlx-community/whisper-large-v3-turbo`（或在网络好的机器上拷贝 `models--mlx-community--whisper-large-v3-turbo` 目录）。
- silero VAD：`silero_vad.onnx`（约 2MB）首次使用时从 GitHub 下载到 `~/.cache/aha_asr/`；无法下载时退回能量法 VAD（效果更差）。离线环境请提前把文件放到该目录。
- 备选后端 SenseVoice（`csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09`）、Qwen3-ASR 权重我们也下载过，但默认不用（SenseVoice 混合中英和日语差，Qwen3 后端未测试）。

## 4. Token

首次运行服务会生成 `~/.aha/token`（权限 0600）。查看：
```bash
ops/aha token
```
这个 token 要抄到 iPhone App 里（做法见 SETUP_IPHONE.md）。不要把它提交进仓库。

## 5. 前台试跑与验证

```bash
ops/aha serve                       # 前台运行，Ctrl-C 停
curl http://127.0.0.1:8765/v1/health
ops/aha doctor                      # 另开终端
```
第一次监听 0.0.0.0 时 macOS 会弹 "Do you want python to accept incoming network connections?"，点 Allow（错过了去 System Settings > Network > Firewall > Options 改）。

注意：之前我们遇到过一个残留的 `python -m http.server 8765` 绑在 127.0.0.1 上抢占 localhost:8765，导致 curl 本机通、手机不通。试跑前 `lsof -nP -iTCP:8765 -sTCP:LISTEN` 确认只有 aha 在监听。

## 6. 装 launchd

先改 plist 里的路径（用户名、仓库位置、`AHA_DATA_DIR`）。
```bash
cp ops/com.aha.server.plist pipeline/com.aha.dispatch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aha.server.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.aha.dispatch.plist
```
不要加载 `com.aha.digest.plist`：它带 `--llm`，会把转写内容发给 Claude API，出于隐私我们没有启用。

验证：
```bash
ops/aha doctor            # launchd / health / disk / queue / aha_asr 应为 OK
tail -f ~/Library/Logs/aha-server.log
```

## 7. 找 Mac 的局域网 IP

```bash
ipconfig getifaddr en0
```
手机填 `http://<ip>:8765`。IP 随网络变化（家里 Wi-Fi 和 Mac 连手机热点时不同，热点下我们遇到过 `172.20.10.3`）。mDNS 名称 `shaoruideMacBook-Pro.local` 在这台机器 / 网络上不能解析，所以 IP 是实际在用的路径。

## 8. 安装 Xcode（只有要给 iPhone 编译 App 时才需要）

我们的情况：macOS 15.6.1，App Store 里的 Xcode 27 需要 macOS 26.6，装不了，所以装 Xcode 26.3。Command Line Tools 不够（没有 iPhoneOS SDK）。

1. 浏览器打开 developer.apple.com/download/all，用 Apple ID 登录。
2. 第一次会看到 "Your Apple ID is not eligible"：去 developer.apple.com/account 接受 Apple Developer Agreement（免费，不需要 $99 的 Program），再回来。
3. 下载 Xcode 26.3 的 Apple silicon 版 `.xip`（约 2.3GB）。
4. 校验并解压（解压到一个空目录，之后挪走）：
   ```bash
   pkgutil --check-signature Xcode_26.3_Apple_silicon.xip
   xip -x Xcode_26.3_Apple_silicon.xip
   mv Xcode.app /Applications/Xcode.app
   ```
5. 手动打开 Xcode 一次：同意 license，等 first-launch 组件安装（会要 Mac 密码）。
6. 装 iOS 平台：
   ```bash
   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -downloadPlatform iOS
   ```
7. `brew install xcodegen qrencode`（xcodegen 生成工程；qrencode 用来把 token 变二维码传给手机）。

我们没有执行 `xcode-select -s`；`ops/ios_reinstall.sh` 里自己设了 `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer`。手动跑 xcodebuild/devicectl 时也要带上这个变量。

验证：`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -version` 有输出；`xcrun --sdk iphoneos --show-sdk-path` 有路径。

## 9. Xcode 里登录 Apple ID（Personal Team）

Xcode > Settings > Accounts > + > Apple ID，登录后会出现 "Personal Team"（免费签名）。`ios/project.yml` 里的 `DEVELOPMENT_TEAM` 是旧账号的 team id，**换 Apple ID 必须改成新的 team id**（在 Xcode 登录后查：`defaults read com.apple.dt.Xcode IDEProvisioningTeamByIdentifier | grep -E "teamID|teamName"`）；bundle id `com.shaorui0.ahamoment` 如果被占用或换账号，也要改成唯一值（同一个文件的 `PRODUCT_BUNDLE_IDENTIFIER`；`ops/ios_reinstall.sh` 里的 `BUNDLE=` 也要同步改）。接下来见 SETUP_IPHONE.md。
