# 《aibase —— AI Agent 工程治理框架》PPT 完整大纲

> 用途：向他人介绍 aibase（AIOS + Reliable AI Engineering Framework）。
> 共 20 页，完整版 45-60 分钟；精简版（去掉 8/13/14/17 页）约 30 分钟。
> 每页包含：页面内容（PPT 文字）+ 视觉建议 + 演讲备注（重点页）。

---

## 第 1 页 · 封面

**标题**：aibase — AI Agent Operating System + Reliable AI Engineering Framework
**副标题**：让 AI 从「会写代码」到「可靠地做成事」
**署名**：hb · MIT License · 2026

---

## 第 2 页 · 背景与动机（为什么需要它）

**一句话**：第一代 AI 模板假设「AI = 会读规则的助手」，2026 年的 AI agent 需要的是「操作系统」。

**页面内容**
- 第一代假设：`AI 工具 → 读规则 → 执行 → 完成`
- 2026 现实：AI 已能自主规划、多步执行、调用工具，但没有治理约束时会：
  - 乱改不该改的文件（生成目录）
  - 忽略系统架构关系（不在上下文里）
  - 无法自评（AI 不能审查自己的工作）
  - 验证失败时没有修复机制
- 结论：定义 AI 如何 **理解目标 → 建模 → 规划 → 执行 → 验证 → 学习**

**视觉**：左右对比图（旧假设 vs 新现实），右半用红色标出四个失控点。

---

## 第 3 页 · 定位：aibase 是什么

**一句话**：不是「AI 配置模板」，是 **AI Agent Operating System** —— 完整工程治理框架。

**页面内容（五大核心创新表）**
| # | 创新 | 说明 |
|---|------|------|
| 1 | 🧠 AIOS 内核 | Governance + Cognition + Execution + Context + Memory |
| 2 | 🤖 角色式 Agent | 按角色（Manager/Coder/Reviewer…）而非按工具定义 |
| 3 | 🔒 Governance 协议 | 明确「AI 不可以做什么」的边界 |
| 4 | 🔁 可靠执行闭环 | Plan → Impact → Execute → Reflect → Verify → Repair |
| 5 | 📊 评估体系 | 成功率 / 返工 / token / 模型对比 |

---

## 第 4 页 · 整体架构（kit 布局）

**一句话**：aibase 自身就是 kit 布局（自举/dogfood），升级 = 整体替换 `kit/`。

**页面内容（目录树）**
```
AGENTS.md         项目入口（25+ AI 工具原生读取）
kit/aios/         AIOS 内核：governance / cognition / execution / context / memory / policy
kit/agents/       7 个角色定义
kit/profiles/     项目类型模板（backend/game-server/unity/unreal/frontend/data/design/novel）
kit/knowledge/    项目知识图谱
kit/cli/          统一控制入口（task / mkproject / persona / protect / sandbox…）
kit/evaluation/   质量评估体系
runtime/          文件即数据库：任务/状态/验证/审查全在文件系统
```

**演讲备注**：强调「文件即数据库」——所有协作状态零依赖、可移植。这就是业界所称的 **Ralph 模式**（文件系统作为 agent 状态载体）：`runtime/` 不是普通目录，而是 agent 的「数据库」——任务/状态/验证/审查/锁/记忆全部落盘，AI 通过读写文件推进协作，零依赖、可移植、可审计。

---

## 第 5 页 · 治理层：AI 不可以做什么

**金句**：治理先于执行——任何 AI 动作前先过 governance 检查。

**页面内容（5 份治理协议表）**
| 协议 | 管什么 |
|------|--------|
| task-policy | 任务状态机；分级治理 fast-path；返工上限 |
| modification-policy | 目录可写/禁写；高风险文件分级 |
| security-policy | Rule of Two；密钥不进提示词；训练退出 |
| risk-policy | 风险分级 P0-P3 与审批 |
| roadmap-policy | 路线图唯一权威与变更控制 |

---

## 第 6 页 · 风险分级与权限模型

**一句话**：治理强度随风险分级，不搞一刀切。

**页面内容**
- **风险分级**：🔴 P0（数据库/支付/auth，必须人工批准）→ 🟡 P1（API 契约/CI，reviewer 审查）→ 🟢 P2（常规）→ ⚪ P3（低风险）
- **自动升级**：改动 >5 文件 / 跨 >2 模块 / 多语言 / 有未解决依赖 → 升一级
- **修改权限**：`generated_dirs` 禁写（改生成代码 = 改规格 → 重新生成）；只有 `source_dirs` 可写

---

## 第 7 页 · 执行闭环（Reliable Execution Loop）

**一句话**：验证是流程的一部分，不是 AI 的自觉。

**页面内容（闭环图）**
```
§0 启动分流（改规格？→ SDD 分支 / 否则通用路径）
§1 Plan → §2 Impact → §3 Execute → §4 Reflect → §5 Verify → §6 Repair
```
- **§0 分流**：任务是否改变外部契约/规格/生成源/跨端协议
- **§5 Verify 机械强制**：build/lint/test/check 真跑，`cli/task verify` 通过才写 VERIFY 记录
- **§6 Repair**：返工上限 3 次，超限人工介入（不无限烧循环）

**视觉**：六环闭环图（环形或线性），§0 作为分叉入口，Verify 用醒目色标「机械强制」。

---

## 第 8 页 · SDD 规格驱动开发（可选详讲）

**一句话**：规格是唯一真相，规格变更先于实现。

**页面内容**
- **三种项目形态**：contract（API 契约+代码生成）/ docs（工具/游戏客户端）/ protocol（CS 网游）
- **五个场景**：A/A'/A"（规格变更）、B（逻辑修改）、C（Bug 修复）
- 核心约束：规格唯一真相 / 生成代码不可手动编辑 / 变更先于实现

---

## 第 9 页 · 角色体系：按角色而非工具

**金句**：新：`Coder 角色需要什么能力？`；旧：`Claude 能做什么？`

**页面内容（7 角色表）**
| 角色 | 职责 | 写代码 | 关键约束 |
|------|------|:--:|------|
| Manager | 任务分派、进度跟踪 | ❌ | 只管流程 |
| Analyst | 需求分析、影响评估 | ❌ | 不明不写 |
| Architect | 架构决策、技术选型 | ❌ | ADR 记录 |
| Coder | 按计划实现 | ✅ | 必须过 verify |
| Reviewer | 代码审查、安全检查 | ❌ | 生成者 ≠ 审查者 |
| Tester | 测试编写执行 | ✅(测试) | 测试先行 |
| Researcher | 外部检索、知识录入 | ❌ | 写入 knowledge/ |

**关键点**：新 AI 工具（Codex/Gemini CLI/Qwen Code…）不断出现，按角色定义无需为每个工具写适配器。

---

## 第 10 页 · 机械强制层：四道墙

**金句**：规则写在文档里是建议，写在文件系统/Git/CI 里才是纪律。

**页面内容**
| 层 | 机制 | 挡什么 |
|---|------|--------|
| 进程沙箱 | `sandbox-run`（无网络容器） | Rule of Two：敏感数据不可外传 |
| 文件权限 | `protect`（chmod 锁 generated_dirs） | 手动改生成代码 |
| Git hook | pre-commit / commit-msg | 无 TASK 引用、碰 generated_dirs |
| CI | GitHub Actions verify.yml | 绕过本地 hook 也挡得住 |

**演讲备注**：这是 aibase 和一般 prompt 模板的本质区别——不靠 AI 自觉，靠系统强制。

---

## 第 11 页 · CLI 工具箱

**页面内容（命令速查）**
- `mkproject` — 创建项目（--profile / --persona）
- `task new|list|start|verify|review|approve|done` — 任务生命周期
- `verify` — 真实执行 build/lint/test/check
- `persona list|use|off|show` — 人格系统
- `check` / `protect` / `sandbox-run` — 健康检查 / 锁目录 / 沙箱
- `autoloop-coder / autoloop-reviewer` — 无人值守（实验性）

---

## 第 12 页 · 知识图谱（knowledge/）

**金句**：AI 最大的问题不是不会写代码，而是不知道系统关系。

**页面内容**
- `architecture/` 架构 · `modules/` 模块清单 · `dependencies/` 依赖图
- `decisions/` ADR · `history/` 已知问题/技术债 · `glossary/` 术语表
- 原则：Knowledge invisible to the agent doesn't exist. / Module before change.

---

## 第 13 页 · 评估体系（evaluation/）

**一句话**：不评估 = 不知道 AI 做得好不好。

**页面内容**
- 指标：成功率 / 返工次数 / Bug 率 / Token / 耗时 / **模型横向对比**
- 用途：任务关闭记 metrics；验证失败记 failures；定期出报告
- 示例：同一任务 Claude Sonnet vs GPT-4o 对比

---

## 第 14 页 · 人格系统（表达层设定）

**一句话**：人格只改语气，不改结论；治理纪律不变。

**页面内容**
- 人格 = 表达层设定（语气/修辞/叙事），**绝不改变**推理、结论、工程判断
- 按需加载：`personas/active.md` 存在则激活，缺失则零加载
- 单源真相 + 薄壳指路；库内 50+ 人格；`严谨模式` 可随时去人格化

---

## 第 15 页 · 快速上手（5 分钟 Demo）

**页面内容（命令行演示）**
```bash
git clone <aibase> /tmp/framework
python /tmp/framework/kit/cli/mkproject ~/code/my-project --profile backend
bash kit/cli/protect
python kit/cli/task new "实现用户登录" --priority P1 --reviewer claude
python kit/cli/task start TASK-001
python kit/cli/task verify TASK-001
python kit/cli/task review TASK-001
python kit/cli/task approve TASK-001
```

**演讲备注**：现场演示或录屏，重点展示 `verify` 是「真跑命令」而不是手写记录。

---

## 第 16 页 · 实际应用：dogfood 实证

**一句话**：框架自己就是自己的第一个用户。

**页面内容**
- **aibase 自举**：自身即 kit 布局，58 个 TASK（任务/审查/验证记录齐全）
- **已同步注册项目**：aimonitor / account / baseline / x1-prototype / x1design / westhill（6+ 个）
- 展示：`cli/task list`、REVIEW 记录、VERIFY 记录、autoloop 心跳日志

---

## 第 17 页 · aimonitor：框架造出的第一个成品

**副标题**：一个实时监控多个 AI 项目的仪表盘，也是框架设计的"活体验证"

**页面内容**
- **它是什么**：读取多个 AIOS 项目的执行状态（任务/进度/心跳/事件流/验证审查），网页实时展示；后端 Python 3.12 纯标准库，零依赖
- **架构图**：被监控项目（runtime/）→ 采集（local 直读 / agent 推送）→ aimonitor 仪表盘（:3113）
- **它证明了四件事**：
  | 牌 | 一句话 |
  |----|--------|
  | ① 框架能造出真成品 | 有前后端 + 多机部署 + 运维手册 + 注册审批流程 |
  | ② 「文件即数据库」被真实消费 | 读的正是 runtime/ 里的任务、心跳、事件流、VERIFY/REVIEW |
  | ③ 跨项目协作不是画饼 | 同时监控 7+ 个 AIOS 项目（含远程 Windows/Linux） |
  | ④ 零依赖哲学可行 | Python 标准库 + 手写前端，无 node_modules/pip 依赖 |
- **小彩蛋**：它自己也是用这套框架开发的（69 个 TASK + MONITOR-SPEC 规格驱动）

**演讲备注**（60-90 秒口播）：
> 「前面讲了框架的设计——文件即数据库、可观测、跨项目、零依赖。有人会问：这些都是纸面上的吗？这一页就是答案。」
> 「aimonitor 是一个网页仪表盘，实时展示多个 AI 项目正在干什么：哪些任务在做、哪些卡住了、autoloop 循环还活着吗、验证审查过了没有。后端只用 Python 标准库，前端手写。」
> 「被监控项目把状态写在 runtime/ 文件里——这正是框架『文件即数据库』的设计。采集层 local 直读本地，agent 遥测组件随 mkproject 自动分发、支持远程机器。」
> 「它证明了四件事：框架能造出真成品；『文件即数据库』被真实消费；跨项目协作已在运行；零依赖不是空话。」
> 「最有意思的是：aimonitor 自己也是用这套框架开发的——69 个任务，docs 型规格。它既是框架的产品，也是框架的镜子。」
> （可选）「环境允许的话，现场打开 http://localhost:3113/ 看实时仪表盘。」

**备选收尾金句**：先别问框架能不能做好软件——它已经做好了一个。

---

## 第 18 页 · 演进路线：从单项目自动到跨项目流水线

**一句话**：AI 负责跑，人负责定方向、批风险、验收果——全自动是手段，可靠是目的。

**页面内容（四阶段表）**
| 阶段 | 内容 | 现状 |
|------|------|------|
| ① 单项目自动闭环 | autoloop coder+reviewer，P0/返工超限转人工 | ✅ 已实现（实验性） |
| ② 多项目监控 | agent 遥测 + aimonitor 仪表盘，心跳/告警 | ✅ 已实现 |
| ③ 多项目自动调度 | 一个调度器读多项目 runtime/tasks/ 分配 coder/reviewer | 🟡 架构已就绪，缺编排层 |
| ④ 点子→成品流水线 | 跨项目 DAG + 阶段人工闸门 + 成品验收 | 🔵 愿景，需逐步验证 |

**已内建的人机分工点**（讲信任/安全时展开）：
- P0 风险操作 → 必须人工批准（approval-ref，否则 blocked）
- 返工 ≥3 次 → 机械拒绝，人工介入
- 卡死/阻塞 → task block + aimonitor 告警（blocked-ratio / stale-task）
- 成品验收 → 当前 verify 只验 build/lint/test/check，业务验收仍需人

---

## 第 19 页 · 与生态标准的关系

**一句话**：不是又一个孤立框架，是与标准生态对接的工程层。

**页面内容**
- **AGENTS.md**：标准格式，25+ AI 工具原生接入，零配置（Claude Code 用 symlink）
- **AIOS（agiresearch）**：设计理念同源（Governance/Context/Memory/Storage），作为文件级治理层独立实现；其中「文件即数据库」对应 AIOS 的 Memory/Storage Manager 设计，即 **Ralph 模式**（文件系统作为 agent 状态载体）
- **MCP**：`tools/` 能力层可包装为 MCP server
- **A2A**：`agents/` 角色可通过 A2A 协议跨框架协作（远期）
- **跨平台**：Windows（PowerShell + python）/ Linux / macOS

---

## 第 20 页 · 总结与 Q&A

**一句话收尾**：治理 + 角色 + 闭环 + 机械强制 + 知识 + 评估 = 可靠 AI 工程。

**设计理念三句话**
1. AI 不是「收到命令→执行」的助手，而是「理解→建模→规划→执行→验证→学习」的执行者
2. 按角色而非工具定义；文件即数据库（Ralph 模式）；治理先于执行
3. 自动化有护栏：低风险自动，高风险人审，超限升级人工

**Q&A**

---

## 附录 · 精简版（30 分钟）页面裁剪建议

- 必讲：1/2/3/4/5/6/7/9/10/11/15/16/17/20（14 页）
- 可省：8（SDD 详讲）、13（评估）、14（人格）、18（演进）→ 各压缩为一句话带过
- 若现场有网络，第 17 页务必带演示（localhost:3113）

## 附录 · 页面对应源文档（备查）

| PPT 页 | 源文档（kit/ 内） |
|--------|------------------|
| 5/6 | aios/governance/*.md（5 份协议） |
| 7 | aios/execution/engine.md |
| 8 | aios/execution/sdd-workflow.md |
| 9 | agents/README.md + agents/<role>/role.md |
| 10/11 | cli/README.md |
| 12 | knowledge/README.md |
| 13 | evaluation/README.md |
| 14 | personas/README.md + personas/active.md |
| 4 | docs/ARCHITECTURE.md（文件即数据库 / Ralph 模式） |
| 17 | aimonitor: README.md + docs/MONITOR-SPEC.md |
| 19 | docs/ARCHITECTURE.md |

## 附录 · 变体与再生成（TASK-078）

| 文件 | 来源 | 页数 |
|------|------|------|
| `docs/PPT-AIBASE-INTRO-claude.pptx` | Claude 工具生成变体 | 17（精简） |
| `docs/PPT-AIBASE-INTRO-deepseek.pptx` | DeepSeek 工具生成变体 | 20 |
| `docs/PPT-AIBASE-INTRO-pptxgenjs.pptx` | `docs/ppt/build_aibase_ppt.mjs` 生成 | 20（深色科技风） |

- 旧版 `docs/PPT-AIBASE-INTRO.pptx` 已于 TASK-078 删除；当前三种成品均为可版本化产物。
- 再生成脚本版 PPT：`cd docs/ppt && npm install && npm run build`（依赖 `pptxgenjs@^4.0.1`，见 `docs/ppt/package.json` / `package-lock.json`）。
- 变体（claude/deepseek）为 AI 工具直接生成，如需保持一致请以脚本版为准重新生成。
