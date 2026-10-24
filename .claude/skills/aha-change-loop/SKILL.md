---
name: aha-change-loop
description: 本仓库每个改动的固定闭环：OpenSpec change → 实现 → 验收 → HTML 报告 + 博客笔记 → 按阶段 commit，验收阶段在 docs/PROCESS.md 的 Progress 表追加一行。用户说「做下一个 change」「按 loop 走」「提交这个改动」，或在本仓库开始任何代码/文档改动时使用。
---

# aha-change-loop

一个 change = 一个 OpenSpec change = **按阶段的若干个 commit**（不一把梭哈）。没有验收证据的 change 不算完成。改写自 finops-agent 的 change loop。

## 步骤

1. **开 change**：`openspec new change c<NNNN>-<slug>`（字母开头所以加 `c`；编号四位递增，看 `reports/` 最大号 +1）。写 proposal / specs / tasks；tasks 里必须有一条「验收」，写明用什么命令证明做完。
2. **实现**：只改 tasks 里列的东西。
3. **验收**：跑 tasks 里的验收命令，输出**原样**存到 `openspec/changes/<id>/evidence.md`。至少包含：`python3 -m pytest -q` 的完整输出、`python3 tools/check_rules.py`、`openspec validate <id>`。失败的测试如实记录，不为了绿去改测试；不是本 change 引起的失败，写明原因。
4. **HTML 报告**：`reports/<NNNN-slug>.html`。内容：改了什么、为什么、验收证据、遗留项。本地 HTML 文件，不发 Claude artifact。先写 md 再 `pandoc -s --metadata title=<slug> in.md -o reports/<NNNN-slug>.html`。
5. **博客笔记**：`notes/blog/<NNNN-slug>.md`。只列核心想法、踩的坑、反直觉的结论，bullet 即可，**不做写作润色**。
6. **按阶段提交**：每个阶段做完就 commit，只 `git add` 该阶段**自己的文件**（不用 `-A`、不用 `.`）。message 首行 `c<NNNN>-<slug> [k/n <阶段>]: <一句话>`，末尾加 Co-Authored-By 行：
   - `proposal`：proposal.md（为什么）
   - `spec+tasks`：specs/、tasks.md（做成什么样、怎么验收）
   - `impl`：代码 / 测试 / 配置（可拆多个 impl commit，每个能独立通过测试）
   - `docs`：文档、README、skill
   - `acceptance`：evidence.md + HTML 报告 + 博客笔记 + `docs/PROCESS.md` Progress 表追加一行（Change / Date / What / Takeaway 取博客笔记第一条 / 报告与笔记链接），正文列验收结论
   没有的阶段跳过，n 按实际阶段数算。
7. **汇报验收**：在 main agent 里告诉用户：通过/失败、关键输出、HTML 报告路径。没汇报不算做完。
8. 完成后 `openspec archive <id>`（可与下一个 change 一起做）。

## 硬规则

- **别人的未提交改动原样保留**：本仓库经常有其他进程的在途修改。commit 前 `git status --short` 看一眼，`git diff --cached --stat` 确认只有自己的文件；不 revert、不 stash、不 `git add -A`。
- **个人数据永不进仓库**：`~/aha_data` 内容、任何转写文字（包括「示例」）、`contexts/daily_records/` 内容、雇主名、公司设备管理细节。测试只用虚构 fixture。提交前跑 `python3 tools/check_rules.py` 必须 OK，再 `git diff --cached` 扫一遍。私有模式放 `.leak_patterns.local`（gitignored）。
- 方法层文档（`docs/METHOD.md` 等）不写绝对 home 路径。
- 给用户看的 markdown 同时出 HTML。
- 报告里的每个数字要能指回 evidence。
- 改了不变量（`docs/METHOD.md` §2）就要同时改它的 enforcer 和测试；没有 enforcer 的不变量在表里如实写「convention」。
