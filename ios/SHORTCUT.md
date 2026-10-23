# Day-1 Quick Input: iOS Shortcut "Aha" (no Xcode)

Result: one tap -> record until you tap stop -> audio POSTed to the Mac -> transcript shown and copied.

Prerequisites: Mac server running on `:8765`; iPhone can reach it (same Wi-Fi: `http://<mac-lan-ip>:8765`; away from home: Tailscale `http://100.x.y.z:8765`). Find the Mac IP: `ipconfig getifaddr en0`. Token: the Bearer token configured on the server.

Action names below: English / 简体中文 (iOS 17/18 UI). Verified against Apple docs/forums: `Get Contents of URL` = `获取 URL 内容` (Apple zh-cn guide), `Show Result` = `显示结果`. NOT verified online (from memory, search the action list by keyword if it differs): `Record Audio` = `录制音频`, `Copy to Clipboard` = `拷贝到剪贴板` (older iOS wrote `拷贝至剪贴板`), `Text` = `文本`, the Record Audio options Quality / Start Recording / Finish Recording. In Get Contents of URL the Method/Headers/Request Body fields are hidden until you tap **Show More** (`显示更多`); Request Body appears only for POST/PUT/PATCH.

## Build the Shortcut

1. Shortcuts app -> **+** (top right) -> name it `Aha`.
2. Put two config values at the top so they are easy to edit later:
   - Action `Text` / `文本`: `http://192.168.1.23:8765` (your server base URL) -> tap the result bubble, **Set Variable** / `设定变量` -> name `Server`.
   - Action `Text` / `文本`: `YOUR_TOKEN` -> **Set Variable** / `设定变量` -> name `Token`.
   (Set Variable actions are named "设定变量" in Chinese UI.)
3. **Record Audio** / `录制音频`:
   - Tap the arrow to expand: **Quality** / `质量` = Normal / `正常` (smaller upload), **Start Recording** / `开始录制` = **Immediately** / `立即`, **Finish Recording** / `完成录制` = **On Tap** / `点按时`.
4. **Get Contents of URL** / `获取 URL 内容`:
   - URL: tap the field, type `Server` variable (insert variable `Server`) then literal `/v1/quick?wait=1`.
   - Tap **Show More** / `显示更多` (or the arrow): **Method** / `方法` = `POST`.
   - **Headers** / `标头` -> Add new header: key `Authorization`, value = text `Bearer ` followed by the `Token` variable. Add another: key `X-Aha-Source`, value `quick` (optional).
   - **Request Body** / `请求正文` = **File** / `文件`; set the File field to the magic variable `Recorded Audio` / `录制的音频` (output of step 3).
   - (If the server instead expects multipart, choose Form / `表单`, add a File field named `file`. The server plan accepts raw body, so use File.)
5. **Get Dictionary Value** / `获取词典值` -> Get `Value` for key `text` in `Contents of URL` / `URL 的内容` (the server returns `{"status","id","text"}`).
   - Response handling of `?wait=1`: HTTP 200 = transcribed (`text` may be `""` if the clip was silent); **202** = still transcribing after 60 s (`{"status":"pending","text":null}`, the transcript will appear on the Mac later); **502** = transcription failed (`{"status":"failed","error":"..."}`, no `text`; audio is kept and can be retried). Shortcuts does not treat non-2xx as an error, so the dictionary just has no `text`: to surface failures add **If** / `如果` `Dictionary Value` (key `text`) `has no value` / `没有值` -> **Get Dictionary Value** key `error` (or `status`) -> **Show Result**.
6. **Copy to Clipboard** / `拷贝到剪贴板` (input: Dictionary Value).
7. **Show Result** / `显示结果` (input: Dictionary Value). Optionally **Show Notification** / `显示通知` instead if you do not want to stop at a dialog.

Test: run from the Shortcuts app, say something, tap Stop (the recorder UI shows a stop button). First run asks for microphone permission and for permission to send data to the server host: tap **Always Allow** / `始终允许`.

Troubleshooting: HTTP 502 = ASR failed on the Mac (see `error`); 202 = Mac still busy. HTTP 401 = wrong token (note the space in `Bearer <token>`); "could not connect" = wrong IP / Mac firewall / not on same network; plain `http` works because Shortcuts does not enforce ATS.

## Launch with one tap

**Action Button** (iPhone 15 Pro and later): Settings -> Action Button (操作按钮) -> swipe to **Shortcut** / `快捷指令` -> **Choose a Shortcut** / `选取快捷指令` -> `Aha`. Press = start recording immediately; press the button again or tap stop to finish.

**Back Tap** (any iPhone with Face ID/iOS 14+): Settings -> Accessibility / `辅助功能` -> Touch / `触控` -> **Back Tap** / `轻点背面` -> **Double Tap** (or Triple Tap) -> scroll to the Shortcuts section -> `Aha`. Works with the case on only if the tap is firm; it can false-trigger in a pocket, so prefer Triple Tap.

**Lock Screen widget** (iOS 16+): long-press the lock screen -> Customize / `自定` -> Lock Screen -> tap a widget slot below the clock -> **Shortcuts** / `快捷指令` -> pick `Aha`. Tapping it runs the shortcut (requires Face ID unlock for the first Get Contents of URL on some versions).

**Control Center** (iOS 18+): Control Center -> **+** -> Add a Control / `添加控制` -> Shortcuts -> `Aha`.

**Siri**: "Hey Siri, Aha" works because the shortcut name is the phrase.

## Changing the Mac IP or token later

Open the shortcut -> edit the two `Text` actions at the top (`Server`, `Token`). Nothing else references the address. When the Mac IP changes (DHCP), consider a DHCP reservation on the router, or use the Tailscale 100.x address, which is stable.

Security note: the token is stored in plain text inside the shortcut (synced via iCloud if Shortcuts sync is on). Use a token that only has access to this Aha server.

## 语音任务 → 提醒事项

现在 App 里「面板 → 待办」一键即可，这个快捷指令只作备选

Result: voice segments the local model classified as `[task]` show up in the iPhone Reminders list `Aha 待确认`. Classification is rough, so everything goes to this review list, never your main list: tick it (done) or move it to a real list yourself.

Data flow: `pipeline/synthesize_tasks.py` (Sonnet, once a day after 10:00; v1 `extract_tasks.py` is retired) writes `~/aha_data/inbox/tasks.json` -> `GET /v1/tasks?undelivered=1` -> Shortcut creates Reminders -> `POST /v1/tasks/ack` marks them delivered (no duplicates next run). The Mac has no iCloud, so the Reminders must be written on the phone; this Shortcut is that step. No app change.

Prerequisite: Reminders app -> create a list named exactly `Aha 待确认`.

Steps (new Shortcut named `Aha 任务`; names: English / 简体中文):

1. `Text` / `文本` = server base URL (e.g. `http://100.x.y.z:8765`) -> **Set Variable** / `设定变量` `Server`. Another `Text` = your token -> `Set Variable` `Token`. (Token on the Mac: `.venv/bin/python ops/aha token`. It is stored in plain text inside the Shortcut, same caveat as the `Aha` Shortcut; you can copy these two actions from it.)
2. **Get Contents of URL** / `获取 URL 内容`: URL = `Server` + `/v1/tasks?undelivered=1`, Method `GET`, Headers: `Authorization` = `Bearer ` + `Token`.
3. **Get Dictionary Value** / `获取词典值`: key `items` from `Contents of URL`.
4. **If** / `如果` `Dictionary Value` `has any value` / `有任何值` (skip everything when nothing is new). Inside:
   1. **Repeat with Each** / `对每个项目重复` item in `Dictionary Value`:
      - **Get Dictionary Value** `title` from `Repeat Item` ; **Get Dictionary Value** `note` from `Repeat Item`.
      - **Add New Reminder** / `添加新提醒事项`: Title = the `title` value, Notes = the `note` value (tap **Show More** / `显示更多` to see Notes), List = `Aha 待确认`. Leave Alert/Due empty.
      - **Get Dictionary Value** `id` from `Repeat Item` -> **Add to Variable** / `添加到变量` `Ids` (first run: put a `List` / `列表` action + `Set Variable` `Ids` before the Repeat so it starts empty).
   2. After the loop (End Repeat): **Get Contents of URL**: URL = `Server` + `/v1/tasks/ack`, Method `POST`, Headers `Authorization` = `Bearer ` + `Token`, Request Body = **JSON**, add field `ids` of type **List** / `列表` whose value is the variable `Ids` (add one list item per ... easier: tap the `ids` value and choose the `Ids` variable).
   3. Optional **Show Notification** / `显示通知`: `已添加提醒事项` (Repeat Results count).
5. Do the ack POST only after the loop so a half-failed run re-delivers (duplicates are better than lost tasks).

First run: iOS asks to allow Reminders access and to send data to the server host -> **Always Allow** / `始终允许`.

Automation (optional): Shortcuts -> **Automation** / `自动化` -> **+** -> **App** / `App` -> `Reminders` -> **Is Opened** / `已打开` -> Run Shortcut `Aha 任务` -> **Run Immediately** / `立即运行` + turn off Notify When Run. Alternative: **Time of Day** / `特定时间` 21:30 (after the 21:00 digest). iOS may still force a confirmation banner for some automations (especially Time of Day / when the phone is locked; "Run Immediately" is not always honored); if so, tap the banner or just run the Shortcut manually (home-screen icon / Back Tap). The Mac must be reachable (same Wi-Fi or Tailscale), else the Shortcut just fails and nothing is lost: the items stay undelivered.

Troubleshooting: 401 = wrong token; empty result = nothing new (check `curl -H "Authorization: Bearer $TOK" '<server>/v1/tasks'`); items appear a few minutes after recording (correct_classify tick).

## 截图 / 照片 → 发到 Aha（Share Sheet）

在任意 App（相册、截图预览、支付 App）里点分享 -> 选 Shortcut `发到 Aha`，把图片交给 Mac 识别。Mac 上只做识别并追加到 `~/aha_data/images/ledger.jsonl`，**不会**自动写进任何别的地方（记账/提醒都要你自己确认）；之后用 `GET /v1/images?since=` 看结果。

Steps (new Shortcut named `发到 Aha`; names: English / 简体中文). 在 Shortcut 设置里打开 **Show in Share Sheet** / `在共享表单中显示`，Share Sheet Input 只勾 `Images` / `图像`：

1. `Text` / `文本` = server base URL -> **Set Variable** `Server`；另一个 `Text` = token -> `Set Variable` `Token`（和 `Aha` Shortcut 里的两个动作一样，可以直接复制）。
2. **Choose from Menu** / `从菜单中选取`，提示 `这是什么？`，四项：`收据` / `资产` / `表格` / `照片`。每个分支里用 `Text` + `Set Variable` `Kind` 设为 `receipt` / `asset` / `table` / `photo`（`receipt` = 付款/小票，`asset` = 银行/券商余额，`table` = 表格，`photo` = 普通照片，用视觉模型写一句描述）。
3. **Convert Image** / `转换图像` Shortcut Input -> `JPEG`（HEIC 也能识别，但转 JPEG 上传小很多）。
4. **Format Date** / `格式化日期` 当前日期，格式 `ISO 8601` -> `Set Variable` `Now`；再一个 **Format Date**，`Custom` `yyyyMMddHHmmss` -> `Text` `img-` + 该值 -> `Set Variable` `ID`（id 要求 8-64 位字母数字/横线）。**Hash** / `哈希`（类型 SHA256，输入 = 转换后的图像）-> `Set Variable` `Sha`（小写十六进制）。服务端按 sha256 去重：同一 id 同内容重传只会返回 `exists`。
5. **Get Contents of URL**: URL = `Server` + `/v1/image/` + `ID` + `?kind=` + `Kind`，Method `PUT`，Headers: `Authorization` = `Bearer ` + `Token`、`X-Aha-Sha256` = `Sha`（小写十六进制）、`X-Aha-Recorded-At` = `Now`、`Content-Type` = `image/jpeg`，Request Body = **File** / `文件`，值 = 转换后的图像。
6. **Show Notification**: `已发到 Aha`。

结果：`201 {"status":"created"}` = 已入队，几十秒后识别完（`photo` 类可能因为 Mac 内存紧张被推迟，之后自动重试）；`200 exists` = 同一张图已经传过；`409` = id 相同但内容不同；`401` = token 不对。在 Mac 上看结果：`curl -H "Authorization: Bearer $TOK" '<server>/v1/images?since=2026-10-01'`，或 `tail ~/aha_data/images/ledger.jsonl`；`needs_review: true` 表示关键数字没能在 OCR 原文里逐字找到，需要人看一眼。
