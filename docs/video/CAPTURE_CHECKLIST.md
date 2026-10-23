# 拍摄与剪辑清单

## 1. 拍摄前环境准备

```bash
cd /Users/rshao/work/context-infrastructure/projects/aha_moment
.venv/bin/python ops/aha doctor          # 全 OK 再拍
.venv/bin/python ops/aha serve           # 或 launchd 已在跑:launchctl print gui/$(id -u)/com.aha.server
ipconfig getifaddr en0                   # 记下 Mac IP(只给自己看,别入镜)
curl http://localhost:8765/v1/health
```

- 手机与 Mac 同一 Wi-Fi;App Settings 里点 "Test /v1/health" 确认通。
- 免费签名 7 天过期:拍前一天用 `ops/ios_reinstall.sh` 或 Xcode 重装一次,别拍到一半打不开。
- 先备份真实数据,再决定用独立 demo 目录:
  ```bash
  AHA_DATA_DIR=$HOME/aha_demo_data AHA_PORT=8765 .venv/bin/python ops/aha serve
  ```
  用干净目录拍,画面里没有你以前的私人录音(推荐)。注意手机端上传的是同一个服务地址,需先停掉正式服务再起 demo 服务。
- 若想演示 dispatch:`AHA_DATA_DIR=$HOME/aha_demo_data .venv/bin/python pipeline/dispatch_quick.py`;"问 Claude" 提前单独试通。
- 手机:开勿扰、关通知预览、电量 > 50%、清掉无关 App 通知;Mac:关通知、隐藏 Dock、终端字号 18–20、深色主题统一。

## 2. 要打码/不入镜的东西

| 项 | 出现位置 | 处理 |
|---|---|---|
| Bearer token | Settings 页、`aha token` 输出、网页登录框、`?t=` 音频 URL | 不要进 Settings 页;网页 token 输入前先登录好再录;URL 栏裁掉 |
| Apple ID 邮箱 | Xcode Accounts / Signing 页、VPN & Device Management 页 | 镜头 18 用模糊或手绘示意 |
| Mac IP / Bonjour 名(`shaoruideMacBook-Pro.local`) | Settings、网页地址栏、`ipconfig` | 地址栏打码,或用 `localhost` 录网页 |
| 公司信息 | 终端 prompt 路径、浏览器书签、Slack 通知 | 新建干净终端 profile 与浏览器 profile |
| 他人声音 | 环境录音 | 在家独自拍;不录路人/同事 |
| 真实待办内容 | todo.md | 只用下面的 demo 台词 |
| `~/aha_data` 历史条目 | 网页列表、`aha today` | 用 demo 数据目录 |

## 3. iPhone 录屏

1. 设置 > 控制中心 > 添加 "屏幕录制"(绿点)。
2. 下拉控制中心 > 长按录屏按钮 > 麦克风选 **关闭**(否则会把 App 的录音源抢走/多一路杂音),点开始,3 秒倒计时后回到 App。
3. 注意:**锁屏期间系统录屏会停止或黑屏**。镜头 4 的"锁屏"用另一台设备/相机实拍手机,或者用录屏拍"按侧键前"和"解锁后",中间用转场连接。
4. 橙点在状态栏一直能看到,可在镜头 3/4/6 中特写。
5. 录完后到 照片 里修剪,AirDrop 到 Mac。
6. 竖屏素材输出短版时可直接用;主片横屏则把手机画面放在中间并加背景模糊。

## 4. Mac 录屏

- `⌘⇧5` > 录制所选部分 > 选项:麦克风 **无**,"显示鼠标点按" 打开。区域固定为 1920×1080。
- 终端镜头(7、11、12、16):
  ```bash
  projects/aha_moment/ops/aha today
  tail -n 5 ~/aha_data/inbox/todo.md
  ls ~/aha_data/inbox/answers/ && cat ~/aha_data/inbox/answers/*.md
  ```
  (用 demo 目录时把 `~/aha_data` 换成 `~/aha_demo_data`)
- 网页镜头(8):`http://localhost:8765/`,用 Chrome 新 profile,字体放大 125%。
- 镜头 16 的测试结果:`projects/aha_moment/e2e/RESULTS.md`(11 行全 PASS)与 `pytest -q` 输出截屏,**不要**现场跑 e2e(8 号用例要 6 分钟)。
- 镜头 17:从已有 Claude Code 会话截取,检查是否露出公司路径/代码。

## 5. Demo 台词(让转写好看)

说话慢一点、离手机 30–50 cm、中英术语清楚。**不要念真实隐私内容。**

- 镜头 5 环境自语(Ambient,2–3 句,每句 5–8 秒):
  1. "今天下午想到一个点子:把每天的碎碎念自动整理成待办。"
  2. "对了,周四之前要把部署文档补完,再看一下延迟的看板。"
  3. (停 3 秒)"嗯,晚饭吃什么呢。"
- 镜头 10 Quick:"待办,明天上午把路由方案发给同事。"(开头清晰说"待办",停半拍)
- 镜头 12 Quick:"问 Claude,一句话解释一下什么是实时率。"
- 镜头 14 用真实素材:`~/aha_data` 里 item `20261012T172735_34e01086`(人走开后的噪声)。旧转写文件 `transcript.mlx-community_whisper-large-v3-turbo.txt` 仍保留乱码,新文件 `transcript.mlx-community_whisper-large-v3-turbo_f2.txt` 为空,两个文件并排截图即可,不需要伪造。
- 说完等 10–20 秒再切到 Mac:ASR 约 9.5 倍实时,一段 60 秒约 6 秒转完,但要等 Ambient 的分片上传(满 60 秒才会切段,拍摄时说话要跨过 60 秒边界,或先静待一分钟)。

## 6. 拍摄顺序(省时间)

1. 先拍 Mac 终端/网页静态镜头(7、8、16)。
2. 再拍手机操作(3、6、10、13),每个操作录 2 条备用。
3. 实拍(1、4、5、19、20)最后,统一光线。
4. 旁白最后录:安静房间,近距离,每段留 0.5 秒余量。

## 7. 剪辑检查清单

- [ ] 主片 ≤ 3:00,短版 ≤ 0:45
- [ ] 前 5 秒有钩子,前 15 秒出现手机画面
- [ ] 每个屏幕镜头 ≥ 3 秒,关键字段(Segments/Pending/Last event、`aha today` 输出)放大或圈出
- [ ] 全片无 token、Apple ID 邮箱、完整 IP、公司路径;逐帧过一遍 Settings 页、地址栏、终端 prompt
- [ ] 提到"本地"处只指录音和识别;"问 Claude/AI digest" 的镜头加字幕"此步会把转写文字发给 Claude"
- [ ] 没有声称:24h 不漏、跨天关联已实现、一天搭完(核对后再说)
- [ ] 字幕:中文硬字幕 + 可选英文 .srt;字号手机上可读
- [ ] 音频:旁白 -16 LUFS 左右,背景音乐 -30 dB 以下;环境录音镜头不压低
- [ ] 色彩/字体统一;终端与网页使用同一主题
- [ ] 导出:1080p H.264;短版另导 1080×1920 竖屏
- [ ] 他人声音/脸:无
