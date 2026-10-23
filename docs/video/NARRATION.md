# 旁白稿

口语化,一句一气。方括号里是镜头编号(对应 VIDEO_PLAN 分镜表)和停顿提示。英文字幕为可选。

## A. 主片(约 2:45,约 560 字,按每分钟 210 字语速)

[1] 想法最爱在你手上有事的时候冒出来。
*Ideas show up exactly when your hands are busy.*

[2] 掏手机,解锁,找到 App,点录音,然后才能开口。走完这几步,那句话早就忘了一半。
*Unlock, find the app, hit record, then talk. By then half the thought is gone.*

[3] 所以我做了一个 App。点一下,它就开始录。
*So I built an app. One tap and it starts recording.*

[4] 锁屏也行,放口袋也行,它在后台一直录。你能看到状态栏那个橙点,这是 iOS 强制的,它不是偷偷录。
*Lock the screen, pocket the phone. It keeps going, and that orange dot is iOS making sure it's never secret.*

[5] (停两秒,留环境声和人声)

[6] 回来看一眼。它每 60 秒切一段,自己传到我家里的 Mac 上。没网就先排队,不会丢。
*Every 60 seconds it cuts a chunk and sends it to my own Mac. No network? It queues.*

[7] Mac 这边用本地的 Whisper 转成文字。你看,时间戳,一句一句。
*On the Mac, a local Whisper turns it into text, timestamped.*

[8] 网页上也能看。点一下能听原声,我随时可以对一对,看它有没有听错。
*There's a web view too. Click to hear the original and check the transcript.*

[9] 整个过程,录音和识别都没有离开过这台电脑。
*Recording and recognition never leave this machine.*

[10] 要交代点事的时候,我就按住说话。比如,"待办,明天上午把路由方案发给同事",松手。
*When I want to hand something off, I hold and talk. "Todo, send the routing plan tomorrow."*

[11] 开头说"待办",它就进待办清单。说"记一下",进笔记。
*Say "todo" first and it lands in my todo list. Say "note" and it goes to notes.*

[12] 说"问 Claude",它把问题交给 Claude,答案写回一个文件。
*Say "ask Claude" and the question goes to Claude; the answer comes back as a file.*

[13] 手机上也留了一份,跟语音备忘录一样,能回放,能拖进度条。
*The phone keeps a copy too, like Voice Memos, with a scrubber.*

[14] 说一个有意思的坑。第一天测的时候我走开了一分钟,房间里只有空调声,它居然给我转出了"《diabetes》《至少》Thank you"。
*Here's a fun bug: in a silent room, it transcribed "thanks for watching".*

[15] 这是语音识别模型的老毛病,一安静就瞎编。我加了三道过滤:没人说话的片段,根本不送去识别;识别出来的文字太重复、太没把握,丢掉;最后再拦一张常见幻觉句的表。原始音频一直留着,随时能重转。
*A classic Whisper habit. Three filters: skip non-speech, drop low-confidence or looping text, then a blocklist. The raw audio always stays.*

[16] 断电重启、重复上传、文件损坏,这些我都写了测试,跑过。
*Crashes, duplicate uploads, corrupted files: all tested.*

[17] 这些东西大部分是我用 Claude Code,派几个子 agent 并行写出来的。
*Most of it was built with Claude Code and a few subagents working in parallel.*

[18] 最麻烦的反而是苹果的免费签名。7 天一过期,就得拿 Xcode 重装一次。
*The most annoying part was Apple's free signing: it expires in 7 days.*

[19] 到这里,它能做的就是:录下来、传过来、转成字、把待办分出去。至于它能不能发现"我这个月反复在想同一件事",那是我接下来想验证的。
*So far: capture, transfer, transcribe, route todos. Whether it can spot what I keep circling back to is what I want to test next.*

[20] 有结果了,我再告诉你。
*I'll tell you what I find.*

---

## B. 短版(40 秒,约 140 字)

[1] 想法总是在你没法打开 App 的时候冒出来。
*Ideas show up when you can't open an app.*

[3-4] 我做了一个:点一下,锁屏、放口袋,它一直录。
*So I built one: tap once, lock the phone, it keeps recording.*

[7] 声音自动传到我的 Mac,本地 Whisper 转成文字,没有任何云。
*Audio goes to my Mac and gets transcribed locally. No cloud.*

[10-11] 想交代事情,按住说"待办",它就进清单。
*Hold and say "todo", and it lands in my list.*

收尾:录音和识别,都不出我这台电脑。
*Recording and recognition never leave my machine.*

---

## C. 用词备忘

- 说"录音和识别不出本机",不说"完全不上云":digest 的 AI 部分和"问 Claude"会把文字交给 Claude。
- 不说"24 小时不间断""不会漏":来电/Siri 后后台可能恢复失败。
- 不说"已经帮我发现了项目/规律",除非你另外有真实输出可展示。
- 避免:"让我们""赋能""不仅……而且……""颠覆""革命"。
