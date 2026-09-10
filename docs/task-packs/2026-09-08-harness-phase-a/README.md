# AIOS 主路径修复任务包 · Phase A

创建日期：2026-09-08。性质：待执行的实施规格，不是修复完成报告。
审计基线：aibase `1adfb26`；AIMonitor `b93d5b2`。执行前记录实际版本与已有差异，不自动回退到基线。

## 目标与边界

修复“选对任务→运行指定任务→报告结果→验证当前产物”的主要错误，建立可独立复验的门禁。完成后仅允许**有人监督、单控制器、每个执行器一次一个项目/任务的试点**。本阶段不授予无人值守发布能力，也不完成分布式执行内核重构。

原报告：[结论与路线](../../AIOS-HARNESS-AUDIT-2026-09-08.md)、[19 项发现](../../audits/2026-09-08-harness/FINDINGS.md)。

任务包只放在 docs，不导入 runtime/tasks，不启动 autoloop，也不占用项目现有 TASK 编号。原因：当前选题与依赖检查存在缺陷；本包使用独立 A01—A13 编号，由人类逐项派发。

## 使用方法

1. 开一个独立实现会话，把 [EXECUTION.md](EXECUTION.md) 中的提示词连同一张任务卡交给执行 LLM。
2. 只加载该任务列出的契约小节与源码，先复现，再实施，再验证；不要让它自由选择下一项。
3. 收取 [HANDOFF-TEMPLATE.md](HANDOFF-TEMPLATE.md) 格式的交付记录。
4. 独立会话审查 diff 与测试；涉及契约的任务应由架构复审确认。
5. 验收人确认依赖完成后，再派下一张卡。实现者不能自己签署 accepted。

推荐先派 A01。前两个检查点分别在 A01 完成后、A02+A03+A04 完成后；不要一次把 13 项都交给一个模型无限循环执行。

## 顺序与任务清单

| 编号 | 任务 | 仓库 | 前置验收 |
|---|---|---|---|
| [A01](tasks/A01-isolated-test-foundation.md) | 隔离测试输入，建立可复验的测试基础 | 双仓库 | 无 |
| [A02](tasks/A02-downlink-http-repair.md) | 收敛 AIMonitor 下行实现并修复真实 HTTP | AIMonitor 为主 | A01 |
| [A03](tasks/A03-explicit-task-dispatch.md) | 贯通指定任务参数，失败停止，拒绝无进展成功 | 双仓库 | A02 |
| [A04](tasks/A04-task-eligibility.md) | 统一依赖、负责人、风险与指定审查者检查 | aibase | A03 |
| [A05](tasks/A05-verification-attempt-state.md) | 使最新验证尝试成为唯一关闭依据 | aibase | A04 |
| [A06](tasks/A06-artifact-bound-verification.md) | 将验证绑定到当前工件、测试和任务验收口径 | aibase | A05 |
| [A07](tasks/A07-complete-task-snapshots.md) | 遥测传递完整任务元数据，不完整时禁止远端调度 | 双仓库 | A02、A04 |
| [A08](tasks/A08-event-sequence-namespaces.md) | 隔离服务端与 agent 事件序号，防止静默丢失 | AIMonitor 为主 | A02、A07 |
| [A09](tasks/A09-read-access-control.md) | 保护监控读接口并接通 UI 与 dispatcher 认证 | 双仓库 | A07、A08 |
| [A10](tasks/A10-fatal-error-classification.md) | 普通输出不再触发服务致命错误误杀 | aibase | A03 |
| [A11](tasks/A11-unbounded-task-ids.md) | 修复 TASK-1000 及更大编号的查找和传播 | 双仓库（各自 ID 消费点） | A04 |
| [A12](tasks/A12-ci-and-gate-repair.md) | 修复干净检出 CI、覆盖率口径和失败传播 | 双仓库 | A01、A02、A03、A04、A05、A06、A07、A08、A09、A10、A11 |
| [A13](tasks/A13-independent-phase-acceptance.md) | 独立端到端验收，加入非引擎岗位样例 | 双仓库 | A12 |

默认按编号串行。依赖是“已独立验收”，不是仅写完代码；检查点未通过不继续。
即使两任务无逻辑依赖，共用文件也不能在同一个工作区同时修改。

本包不启动并行 agent。日后由你人工安排并行时，仅将不同文件域放入不同分支/worktree，按冲突表逐项合并：

| 易冲突文件域 | 涉及任务 | 集成约束 |
|---|---|---|
| AIMonitor monitor_server.py | A02 / A07 / A08 / A09 | 必须串行集成；禁止各自重排整个大文件 |
| AIOS tasklib / task CLI | A04 / A05 / A06 / A11 | 必须串行集成 |
| coder / reviewer / launcher | A03 / A04 / A10 | 必须串行集成 |
| dispatcher / telemetry | A03 / A07 / A09 / A11 | 必须串行集成 |
| 两仓库测试与 CI | A01 / A12 / A13，其他任务新增测试 | 测试文件归属随任务；最终以 A13 的双仓库版本组合验收 |

## 岗位独立性

后端、策划、美术、QA 等可在各自项目和工具环境独立工作，不要求进入引擎。岗位任务验收与游戏集成验收分开，详见 [ROLE-INDEPENDENCE.md](ROLE-INDEPENDENCE.md)。A13 包含三种非引擎工件正负用例；真实 DCC 与跨岗位自动调度不在 Phase A 实现范围。

## 固定设计与复审点

设计口径在 [CONTRACTS.md](CONTRACTS.md)，验收矩阵在 [ACCEPTANCE.md](ACCEPTANCE.md)。
执行者可以选择局部函数划分，不能自行改变 HTTP 字段、权限语义、证据范围、错误策略或阶段边界。
认为契约不可实现时，提交“冲突证据+替代方案+兼容影响”，停止该任务等待复审。

特别注意：A02 收敛的是下行队列，A03 才收紧“必须指定一个任务”；A05 处理验证尝试的新旧关系，A06 再绑定产物指纹。这些中间版本不得单独部署。

## 当前进度

所有任务均为 planned，尚未执行、测试或验收。任务卡内的复选框是待验收项，不是预先通过的断言。
本包创建时仅新增任务说明；原审计观察仍保留，不能覆盖成“已修复”。

后续阶段 B/C 仍需事务领取、租约/fencing、进程树、持久回报、独立验证权限和预算等设计，详见 ACCEPTANCE 的剩余风险。Phase A 通过不代表整个 harness 生产合格。
