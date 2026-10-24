# 需求分析：Aha Moment

来源：`PLAN.md`（2026-10-09 原始设计与里程碑，保持原样）、`README.md`（「为什么」与流水线表，现状的唯一事实来源）、`docs/DEVELOPER.md`（不变量与契约）。个人录音原文不进本仓库。

## 1. 一句话

一个按钮把说出来的话变成文字，文字每天堆进个人 AI 工作区（context infrastructure），让之后每个 agent session 一开始就站在「我说过什么、想过什么」之上。录音 app 只是入口，真正的产物是被整理、可审计、会升格成规则的 context。

## 2. 需求拆解

| # | 需求 | 来源 | 状态 |
|---|---|---|---|
| R1 | **一个入口**：手机上一个按钮，不选分类、不起名；摩擦就是成本，每多一步记录次数就少一截 | README「为什么」 | 在用 |
| R2 | **一个出口**：结果回到 `contexts/` 的文字文件、每晚的邮件、App 的面板；入口出口越少信息越集中，LLM 才能跨时间关联 | README「为什么」 | 在用 |
| R3 | **不丢录音**：App 被杀、断网、来电打断都不丢片段；服务端 ACK 前手机不删 | PLAN 不变量 1、DEVELOPER 3.1 | 在用 |
| R4 | **转写在本机**：Whisper 跑在自己的 Mac 上，音频不出这台机器；ASR 后端可插拔 | PLAN M0/M1 | 在用 |
| R5 | **audio 是 source of truth**：transcript 是可重算派生物，永不覆盖，换模型 = 新增文件 | PLAN 不变量 3 | 在用 |
| R6 | **一段录音一个文件**：`daily_records/YYYY-MM-DD_HHMM_aha.md`，同分钟加 `-2`，文件里只有 ASR 文字，不加标题、不加元数据 | README、`pipeline/export_to_context.py` | 在用 |
| R7 | **原文不动，纠错是派生**：错词纠正、标点分段、task/thinking/diary/smalltalk 标签写到 `enhanced/`，永不回写原文 | `aha_correct_classify` | 在用 |
| R8 | **无损分主题再落地**：先分主题（只分类、不压缩），再找知识库里的去处，只追加地写入 | `voice_topic_sort`、`aha_daily_triage` | 在用，手动触发 |
| R9 | **静音不送 ASR**：VAD 在前，避免 Whisper 对静音幻觉 | PLAN 不变量 4 | 在用 |
| R10 | **LLM 步骤只读 enhanced/转写**：日报、任务合成、主题归类的输入是文字文件；observer 按设计只看到「新增了文件」，不读内容 | `periodic_jobs/ai_heartbeat/docs/PRD.md` | 在用 |
| R11 | **每晚与每周的沉淀**：observer 每晚、reflector 每周，把站得住的 pattern 升格；L3 规则每个 session 先读 | AGENTS 根文件「Memory System」 | 在用（升格到 rules/ 的比例未度量） |
| R12 | **跨天合并同一想法**：同一个想法在不同天重复说，应被识别并合并，而不是记成两条 | 本文档新增 | 方向 |
| R13 | **voice 内容进入 L1/L2**：现在语音内容通过 triage 落到决策文档/项目目录，但 observer 看不到内容；需要一条显式、可审计的通道 | 本文档新增 | 方向 |
| R14 | 语音通道只读：语音触发的 Claude 任务只写 OpenSpec proposal，可读 `~/aha_data`，不写；远程时不碰集群 | PLAN 2026-10-18 补充 | 在用 |
| R15 | **证据可运行**：不变量要有测试或脚本，pytest 原样输出放进 evidence，失败如实记录 | 本次（c0001/c0002） | 本仓库 |
| R16 | **过程即交付**：每个改动一个 OpenSpec change，按阶段 commit，有验收证据、HTML 报告、博客笔记 | 本次，照搬 finops-agent | 本仓库 |

## 3. 交付物（本次 c0001–c0002）

| ID | 交付物 | 验收 | 对应 |
|---|---|---|---|
| D1 | `docs/METHOD.md`：飞轮图、步骤表、三层记忆、不变量、失败模式、「How this proves context engineering」 | 四部分齐全；实线/虚线区分保留 | R1–R13 |
| D2 | `docs/REQUIREMENTS.md`（本文） | 含数据边界 | R15 |
| D3 | `tools/check_rules.py` 泄漏扫描 | 输出 `check_rules: OK`；植入泄漏时失败 | §4 |
| D4 | export 不变量测试（pipeline/tests 下，c0002 新增） | pytest 通过 | R5 R6 |
| D5 | aha-change-loop skill（.claude/skills 下）与 docs 下的 PROCESS.md（c0002 新增） | Progress 表有行 | R16 |

## 4. 数据边界（硬约束）

不得进入本仓库（git）的内容：

- `~/aha_data/` 下的任何内容：原始音频、逐条转写、digest、inbox。
- 任何转写文字，包括贴在文档、测试、报告里的「示例」；测试只用虚构 fixture。
- `contexts/daily_records/` 下的内容（`daily_records/` 在工作区侧已 gitignore）。
- 雇主名、雇主内部系统名、公司设备的管理与监控细节。
- 个人账号、token、邮箱以外的个人信息。`~/.aha/token` 永不入库。
- 方法层文档（docs 下的 METHOD、REQUIREMENTS、PROCESS，以及 openspec/、reports/、notes/）里不写绝对 home 路径。

可检查的部分由 `python3 tools/check_rules.py` 执行：公开模式（AWS 账号号、access key）、`.leak_patterns.local` 里的私有模式（gitignored，仓库里不出现被防的词）、方法层的 home 路径、树里出现的音频与转写文件（`asr/samples/` 的合成 TTS 基准音频除外）。不可机检的部分（「这句话是不是真实转写」）靠提交前 `git diff --cached` 人工扫一遍。

## 5. 设计约束

- **摩擦就是成本**：任何新功能不得给入口增加操作；入口只有一个按钮。
- **LLM 不碰 source of truth**：只写派生物；派生物可删可重算。
- **只追加**：落地到决策文档、memory 的动作只追加，不改写已有内容。
- **本地优先**：转写不出机；需要联网的是纠错/日报这类只读文字的步骤，失败时保持 pending，下个 tick 重试。
- **不碰别人的改动**：本仓库有其他进程的未提交改动时，只 `git add` 自己的文件。
