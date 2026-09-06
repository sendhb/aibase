# ADR — Harness 正规化收口（8 项构件复评与架构定案）

- **状态**：Accepted（2026-09-05）
- **出处**：2026-09-03 AIOS 架构 harness 正规度审计（对照"正规 Harness"标准 8 项构件清单）
- **关联**：`docs/ROADMAP.md` Phase 4（Harness 正规化收口）；TASK-092..098（7/7 收口门 = 本 ADR）
- **确认**：hb（人工）；记录会话：TASK-098（autoloop-coder）

## 1. 背景（审计发现）

2026-09-03 架构审计以"正规 Harness"标准对照全仓，清单 8 项：**Agent Loop / 上下文工程 / 工具层 ACI / 状态与记忆 / 治理护栏 / 确定性验证 / 评估体系 / 编排**。当时结论：

- **已 code 态**：治理护栏（policy + tasklib 闸门）、确定性验证（verify/check）。
- **spec 态/空壳态缺口（5 处）**：评估无自动采集（指标全靠手填）、上下文装配靠军规不靠军械（五策略目录全空壳）、Reflect 纯自省、tools 能力层未 MCP 化（ARCHITECTURE.md 承诺未兑现）、`kit/tools/agent/` 命名误导（实为遥测守护进程）。

→ Phase 4 立项 TASK-092..098 逐项收口。本 ADR 固化收口后的架构决策与 8 项复评结论。

核心判断：**AIOS 是 Agent Operating System（编排层 + 治理层 + 落盘层），不是自研 LLM 运行时**。内循环（模型推理、工具调用、上下文压缩）由宿主 CLI（pi / Claude Code / Codex CLI / …）承担；框架专注宿主做不了或做不稳的事——纪律、状态、验证、评估、编排。

## 2. 决策

### D1 — 内循环外包给宿主 CLI

不自研内层 agent loop（模型调用 / 工具执行 / 会话压缩）。框架的"loop"是 **autoloop：以宿主 CLI 会话为一等公民的外层编排**（拉任务 → 装配上下文 → 跑会话 → verify → 收尾/重试/断点恢复）。宿主可替换，框架不绑定任何一家。

### D2 — 机械管纪律，prompt 管判断

能机械化的纪律一律代码化：上下文预算截断（context_loader）、越界拒绝（Filter/reflect/protect）、P0 闸门与 rework 上限（tasklib）、diff 技术债扫描（reflect_check）、评估自动采集（tasklib 钩子）。判断性工作（架构合理性、测试充分性、需求理解）留在 prompt 层自省与人工 review——两者边界不混淆：**机械项有否决权（verify），prompt 项只有建议权**。

### D3 — file-as-state

跨会话状态只能来自落盘产物：`runtime/tasks/` 前言（状态机）、`runtime/states/STATE-PROGRESS-*`（checkpoint）、`runtime/logs/`（token/sessions/fail）、`kit/evaluation/*.jsonl`（指标）。"清洁上下文"原则（新任务新会话）成立的前提是落盘可靠——框架不依赖会话内记忆传递状态。

## 3. 8 项构件复评表（2026-09-05，TASK-098）

三态定义：**code 态** = 有具体 .py/可执行入口且被验证体系真实执行；**spec 态** = 只有文档定义无代码；**宿主承担** = 选型由宿主 CLI 提供，框架不实现（明确不做，非缺口）。

| # | 构件 | 态 | 证据（code 态指向具体文件） |
|---|------|-----|------|
| 1 | Agent Loop | code 态（外层编排）＋内层宿主承担 | `kit/cli/lib/autoloop_launcher.py`（746 行：round 驱动/超时/事件）、`kit/cli/lib/autoloop_coder.py`（620 行：pick/run_once/收尾 checkpoint）、`kit/cli/lib/autoloop_reviewer.py`（330 行）。内层 LLM loop = 宿主 CLI 承担（D1） |
| 2 | 上下文工程 | code 态（Compress=宿主承担） | `kit/cli/lib/context_loader.py`（246 行：collect/filter/rank/allocate）+ 薄入口 `kit/cli/context`；五策略→代码入口映射见 `kit/aios/context/README.md`（TASK-094）。Compress 由宿主 `/compact` 等承担 |
| 3 | 工具层 ACI | code 态 | `kit/tools/mcp/server.py`（399 行：stdio JSON-RPC 2.0，5 tools 原生暴露 task 状态机 + context_assemble，TASK-097，纯 stdlib）；`kit/tools/dispatcher/`（8 文件 1814 行）；`kit/cli/sandbox-run`（Rule of Two 执行隔离） |
| 4 | 状态与记忆 | code 态（file-as-state） | `kit/cli/lib/tasklib.py`（574 行：runtime/tasks 前言状态机）；`kit/cli/lib/autoloop_coder.py` write_checkpoint（正常/超时/异常三路径落盘 STATE-PROGRESS，TASK-095）；`runtime/logs/sessions/<TASK-ID>/`（会话全量落盘，TASK-103）；`kit/evaluation/*.jsonl`。记忆=文件系统（`kit/aios/memory/README.md` 五类记忆映射，无独立记忆服务——按 D3 这是定案不是缺口） |
| 5 | 治理护栏 | code 态（policy=文档，闸门=代码） | `kit/aios/governance/`（task/modification/security/risk/roadmap 5 policy）；机械闸门：`kit/cli/lib/tasklib.py`（P0 approval-ref 强制、rework 上限 2→3 拒绝、TRANSITIONS 状态机）+ `kit/cli/protect`（generated_dirs OS 只读锁，chmod 强制不靠自觉）+ `kit/tools/dispatcher/governance.py`（下行任务治理） |
| 6 | 确定性验证 | code 态 | `kit/cli/task` cmd_verify（L335 起：build/lint/test/check 四道全跑 + VERIFY 记录自动生成 + 失败自动写 `evaluation/failures/`）；`kit/cli/check`（环境健康检查）；`kit/cli/lib/precommit_check.py` |
| 7 | 评估体系 | code 态 | `kit/cli/lib/tasklib.py` collect_done_metrics（done 钩子自动写 metrics）+ `kit/cli/task` cmd_metrics（汇总：done 率/平均 rework/token 合计）+ `kit/evaluation/metrics/task-metrics.jsonl`（**7 条真实采集**：TASK-092/093/094/095/096/097/103）+ `kit/evaluation/failures/failures.jsonl`（verify 失败自动采集）。TASK-093 交付 |
| 8 | 编排 | code 态 | `kit/tools/dispatcher/dispatcher.py`（565 行：scan/allocate/downlink/dispatch 跨项目调度）+ `policy.py`/`state.py`/`monitor.py`/`probe.py` 等；单项目任务环 = `kit/cli/lib/autoloop_launcher.py`。Phase 3 已多项目闭环验证（TASK-068..084） |

**审计"明确不做"清单复核**（非 8 项，维持原判）：cognition 机械化（`kit/aios/cognition/` 保持 spec 态，职责在 prompt 层——D2 边界）、自研内层 agent loop（宿主承担——D1）、A2A（远期，`kit/aios/protocol/` 已声明，MCP 已满足当前互通）、RAG/嵌入检索（远期，知识库超万行再议，文件系统级装配已够用）。

**结论：8/8 达成"code 态或明确标注宿主承担"，无裸 spec 态残留。**

## 4. 后果

**正面**：

1. **零第三方依赖、宿主可替换**：全框架 stdlib（MCP server 8 个行首 import 全 stdlib，单测护栏）；换宿主 CLI 只换 executor，治理/状态/验证/评估资产全部保留。已实测 pi 与 Claude Code 双宿主。
2. **纪律不靠自觉，弱模型不溃堤**：预算截断、generated_dirs 只读锁、P0 闸门、rework 上限、verify 四道全是机械执行；模型再弱也无法绕过 OS 层与状态机约束。
3. **状态可审计、可恢复、可度量**：file-as-state 使断点恢复（TASK-095 演练闭环）、审查取证（REVIEW/VERIFY 记录）、质量度量（metrics 自动采集 7+ 条真实数据）共享同一落盘源，无第二真相源。

**负面**：

1. **能力上限受宿主制约**：上下文压缩质量、工具调用可靠性、长会话稳定性取决于宿主 CLI；框架无法补救宿主的内层缺陷，宿主升级/降级直接体现在产出质量上。
2. **机械规则覆盖不到判断性质量**：架构合理性、测试充分性、需求理解偏差仍是 prompt 层自省 + 人工 review 兜底；reviewer 质量成为质量上限，无法完全自动化。
3. **落盘冗余与维护成本**：jsonl/日志/checkpoint 持续增长（sessions 全量落盘尤甚），需要定期归档策略；file 轮询与多进程写入有延迟与锁竞争成本（lock.py 已缓解但未消除）。

## 5. 生效与后续

- 本 ADR 即 Phase 4 验收门交付物；ROADMAP Phase 4 完成标志核对结论记录于 `runtime/tasks/TASK-098-*.md` 备注（ROADMAP 本体由 Manager 唯一落盘）。
- 后继 Phase 5（点子→成品流水线）以本 ADR 的 D1/D2/D3 为架构前提；若推翻任一决策须新开 ADR 并回溯影响 TASK。
