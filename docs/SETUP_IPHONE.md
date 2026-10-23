# 新 iPhone 接入清单

前提：Mac 端已按 SETUP_MAC.md 完成（服务在跑、Xcode 26.3 + iOS 平台已装、Xcode 里登录了 Apple ID）。每步后面的"验证"是你怎么知道这一步成了。

## A. 一次性准备（换手机时做）

1. **USB 直连 Mac**。用原装线直接插 Mac，不要经过 hub（hub 可能只供电不通数据）。解锁手机，弹出"信任此电脑？"点 **Trust / 信任**，输入手机密码。
   验证：`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun devicectl list devices` 里能看到这台 iPhone。
2. 配对（若 list 里状态不是 connected / paired）：
   ```bash
   DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun devicectl manage pair --device <设备名或 id>
   ```
3. **开启 Developer Mode**：手机 Settings > Privacy & Security > 最底部 **Developer Mode** > 打开 > 手机会提示重启 > 重启后解锁时再确认一次 "Turn On"，输入密码。
   验证：Developer Mode 开关为开；该选项只有在 Mac 用 Xcode 连过手机之后才会出现，找不到时先完成第 1 步并在 Xcode 里打开一次工程。
4. **首次签名构建必须对着真机**（免费 team 需要先注册设备）。直接用脚本：
   ```bash
   cd ~/work/context-infrastructure/projects/aha_moment
   ops/ios_reinstall.sh
   ```
   脚本做的事：找到 USB 上的 iPhone，`xcodegen generate`，`xcodebuild ... -allowProvisioningUpdates -allowProvisioningDeviceRegistration`（这个参数让 Xcode 自动把设备注册到 Personal Team），`devicectl device install app`，再启动。
   手机要保持**解锁**：锁屏时挂载开发者镜像会失败（`kAMDMobileImageMounterDeviceLocked`）。
   换了 Apple ID 的话，先改 `ios/project.yml` 的 `DEVELOPMENT_TEAM`（见 SETUP_MAC.md 第 9 步）。
   验证：脚本最后输出 `OK: signed for 7 more days (<日期>)`，手机桌面出现 "Aha" 图标。
5. **信任开发者 App**：第一次打开会报 "Untrusted Developer"。手机 Settings > General > **VPN & Device Management** > 你的 Apple ID（Developer App 下）> **Trust**。再次打开 Aha。
   验证：App 正常打开，显示 "Aha Moment" 主界面（Ambient OFF 按钮、Recordings、Hold to talk (Quick)）。

## B. 配置 App

6. 打开 App，点右上 **Settings**，Server 区：
   - 第一个框（Primary）保持默认 `http://shaoruideMacBook-Pro.local:8765`（在这台 Mac / 网络上其实不通，保留作为首选，3 秒无响应自动用 Backup）。
   - **Backup** 填 Mac 的局域网 IP：`http://<mac-ip>:8765`。Mac 上 `ipconfig getifaddr en0` 查。Mac 连 iPhone 个人热点时我们用的是 `http://172.20.10.3:8765`，IP 每换一个网络就变。
7. **传 token**。Mac 上生成二维码（token 不会出现在终端历史里被别人看到的话更好；这里命令会在 /tmp 写一张图，用完删掉）：
   ```bash
   tr -d '\n' < ~/.aha/token | qrencode -s 12 -o /tmp/t.png && open /tmp/t.png
   ```
   iPhone 打开相机对准屏幕，长按识别出的文字 > Copy，回到 App 的 **Bearer token** 框粘贴。用完 `rm /tmp/t.png`。
   token 现在在编辑时就会保存（早期版本只有点 Done 才保存，已修复）。
8. 点 **Test /v1/health**。
   验证：下面显示服务返回的 JSON（health 信息）。失败时：先用手机 Safari 打开 `http://<mac-ip>:8765/v1/health`，能出 JSON 说明网络通，问题在 App 配置；打不开说明手机和 Mac 不在同一网络、IP 填错，或 Mac 防火墙拦截。
9. 点 **Done**。

## C. 权限

10. 第一次开 Ambient 或按 Quick 时依次允许：
    - **Microphone**（麦克风）：Allow。
    - **Local Network**（本地网络）：Allow。否则局域网上传一直失败。事后可在 Settings > Aha > Local Network 打开。
    - **Notifications**：Allow（录音意外停止时会发 "Aha 录音已停止 — 点按恢复"）。
    验证：状态栏出现橙色麦克风点（iOS 强制指示，录音期间一直亮）。

## D. 端到端验证

11. 点 **Ambient OFF** 变为 **Ambient ON**，说几句话，等 1 分钟（一个片段 60 秒）。
12. 主界面右上/左上：**Queue**（左上）里 Pending 应回到 0，"Last ACK" 有时间。
13. Mac 上：
    ```bash
    ops/aha status
    ops/aha today        # 约 1-2 分钟后能看到刚才说的话
    ```
    验证：`today` 输出带时间戳的文字。这时可再点一次变回 Ambient OFF。

## 备选：不装 App，只用 iOS 快捷指令

见 `ios/SHORTCUT.md`（一键录音 -> POST /v1/quick?wait=1 -> 显示并复制转写）。不需要 Xcode，也没有 7 天过期，但没有后台持续录音。

## 常见卡点（本次真遇到的）

| 现象 | 原因 / 处理 |
|---|---|
| list devices 看不到手机 | 用了 hub 或线不通数据；直连 Mac，解锁，点 Trust |
| 安装时报 `kAMDMobileImageMounterDeviceLocked` | 安装过程中手机锁屏了；保持解锁重跑 |
| 设置里找不到 Developer Mode | 手机还没和装了 Xcode 的 Mac 配对过；先做第 1-2 步 |
| 构建报 team / provisioning 错误 | 第一次必须带着真机构建；或 `project.yml` 的 team id / bundle id 不是你这个 Apple ID 的 |
| 7 天后 App 打不开 | 签名过期，见 OPERATIONS.md "7 天重装" |
