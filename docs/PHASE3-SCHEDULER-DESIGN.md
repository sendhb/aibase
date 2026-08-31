# Phase 3 调度器 — 范围定义 + 架构设计（TASK-068）

> 状态：设计稿（TASK-068，已提交审查）
> 关联：`docs/ROADMAP.md` Phase 3（多项目自动调度）；来源：PPT 演进路线四阶段表 ③ + glossary Harness 选型结论（TASK-064）。
> 硬约束：中央不直写远端文件系统；下行指令由目标项目本地 agent 落盘 `runtime/tasks/`（Ralph 跨项目成立）。

## 一、现状盘点（设计输入）

| 能力 | 现状 | 出处 |
|------|------|------|
| 项目注册表 | `aimonitor/config/projects.json`：10 条注册——6 条同机本地项目（`/home/hb/code/*`，默认 transport）+ 4 条远端 `D:/share/*`（`"transport": "agent"`） | TASK-054 定为权威注册表 |
| 单项目任务状态机 | `kit/cli/task`：TASK-*.md frontmatter，open→in-progress→in-review→done | TASK-001.. |
| 单项目自动执行 | `kit/cli/autoloop-coder`：单项目内捡 open/in-progress 任务，锁防并发 | TASK-012..021 |
| 上行遥测 | `kit/tools/agent/`：读 runtime/ → 推 aimonitor（心跳/事件流/游标） | TASK-022..029, 042..044, 065..066 |
| 审查闭环 | `autoloop-reviewer`：独立进程，只经 runtime/ 文件耦合（生成者≠审查者） | TASK-014..017 |
| 跨机部署 | `docs/DEPLOY-DUAL-PLATFORM.md`：Windows/Linux 双平台 | TASK-035, 052 |

**缺口（Phase 3 要补的）**：一个中央调度器——跨项目读状态、决定「哪个项目该干活」、触发目标项目的本地执行链、回收/超时/告警。目前 6 个项目各自为战，无人统筹。

## 二、范围定义

### 做（In Scope）
1. **调度器主体** `kit/tools/dispatcher/`：读注册表 → 扫描各项目 runtime/tasks/ 状态（只读）→ 按策略选出需要执行的任务 → 下行触发 → 跟踪结果
2. **下行触发**（Downlink）：在目标项目**本地**执行既有工具链（`task new/start`、`autoloop-coder --once`），让 `runtime/tasks/` 由项目自己的 agent 落盘——不越权直写
3. **调度策略**：基础公平轮转 + 优先级 + 并发上限（每项目同时 ≤1 个 worker，全局 ≤N）
4. **回收与超时**：任务卡死/worker 崩溃 → 超时标记 → 告警/重新分配；stale-task 检测
5. **治理挂钩**：P0 任务保持 blocked 直到人工 approval-ref；返工 ≥3 次机械拒绝转人工；跨项目仍然生效
6. **可观测性**：调度器自身状态可推 aimonitor（与 agent 同构）；调度事件写事件流

### 不做（Out of Scope）
- ❌ Phase 4：跨项目 DAG、阶段人工闸门、成品业务验收（另立阶段）
- ❌ 中心数据库 / 共享任务存储（零依赖哲学，Ralph 不变）
- ❌ 中央直写各项目 `runtime/tasks/`（硬约束，物理禁止——通过本地执行链落盘）
- ❌ 直接协议级 A2A（远期为调度器适配层可升级，本阶段不做）
- ❌ 重写 autoloop/task CLI 内部逻辑（复用，不重造）

## 三、架构设计

### 组件

```
┌───────────────────────── 中央（调度器，可跑在 aimonitor 机） ─────────────────────────┐
│  kit/tools/dispatcher/dispatcher.py                                                    │
│    ├─ registry.py     读 projects.json 注册表                                           │
│    ├─ probe.py        只读探测各项目 runtime/tasks/ 状态（复用 agent_runtime 只读层）    │
│    ├─ policy.py       选任务策略：优先级/公平轮转/并发上限                               │
│    ├─ downlink.py     下行执行适配器（本地 subprocess；未来 SSH/HTTP 换适配器）           │
│    ├─ state.py        调度状态机（分配/回收/超时/重试）                                  │
│    └─ monitor.py      调度事件 + 心跳 → aimonitor（同 agent 协议）                      │
└───────────────────────────────────────────────────────────────────────────────────────┘
                              │ 下行（downlink，本地执行链）
                              ▼
┌──────────────────────── 目标项目（每个注册项目一套） ────────────────────────┐
│  ① task new/start          → 写 runtime/tasks/TASK-xxx.md（本地 CLI 落盘）   │
│  ② autoloop-coder --once   → 单轮实现 + verify + review（复用既有循环）        │
│  ③ autoloop-reviewer --once→ 独立审查（生成者≠审查者）                         │
│  ④ agent（已在跑）         → 上行推送到 aimonitor（调度器可读回）               │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 数据流

1. **上行（已存在）**：各项目 agent 推心跳/任务状态/事件流 → aimonitor；调度器 `probe.py` 直接读 `runtime/tasks/`（同机）或经 aimonitor 状态（跨机）
2. **下行（新增）**：`downlink.py` 在目标项目目录内执行命令——
   - 需要新任务：`python3 kit/cli/task new "..." --priority ... --assignee <worker>`
   - 需要干活：`python kit/cli/autoloop coder --once`（或兼容 shim `python kit/cli/autoloop-coder --once`）
   - 需要审查：`python kit/cli/autoloop reviewer --once`（或兼容 shim `python kit/cli/autoloop-reviewer --once`）
   - 结果以 exit code + 项目事件流（task-events.jsonl）回报，调度器跟踪
3. **闭环**：调度器探测到 open → 分配 → 触发执行 → 探测到 done/in-review → 触发审查 → done → 记录指标

### 注册表处理（v1 明确边界）

- **只处理同机本地条目**（`/home/hb/code/*`，transport 为空/默认）：probe 直接读、downlink 本地执行
- **远端 agent 传输条目**（`D:/share/*`，`"transport": "agent"`）：v1 **跳过并告警**，不尝试本地 subprocess（会因路径不可达失败）
- **registry 适配器**：`registry.py` 必须暴露 transport 字段；TASK-069 验收标准包含「远端条目跳过 + 告警」用例

### 调度策略（v1 从简）

- **选择**：按注册表顺序 round-robin；项目内按 TASK 编号升序（与 autoloop 一致）
- **并发**：每项目 1 个执行 slot（复用 autoloop 进程锁天然防重）；全局并发上限 `--max-workers N`
- **超时**：任务 in-progress 超过 `--task-timeout`（默认 30min）→ 标记 stale → 告警；连续失败 3 次 → 转人工
- **治理**：P0 无 approval-ref → 跳过并告警（同 autoloop 行为）；返工 ≥3 → 拒绝 + 人工
- **公平**：v1 不搞权重/配额，round-robin 即可；配额留给 Phase 4 DAG

### 部署形态

- **同机（v1 主场景）**：所有项目在 `/home/hb/code/` 下，调度器以本地 subprocess 触发，`downlink.py` = 简单执行器
- **跨机（v2 演进）**：`downlink.py` 抽象成适配器——本地 exec / SSH exec / （远期）A2A 消息；调度器主体不变
- **常驻**：`dispatcher.py --daemon`（复用 autoloop 的锁+心跳模式），或 systemd/Task Scheduler

## 四、风险与治理

| 风险 | 级别 | 缓解 |
|------|------|------|
| 调度器误判任务状态导致重复执行 | P1 | 复用每项目 autoloop 进程锁；调度状态机记录分配指纹（task+worker+时间戳） |
| 下行命令在错误目录执行 | P1 | downlink 执行前校验项目路径 + 注册表一致性 |
| 跨机后下行通道安全问题 | P2 | v1 只做同机；跨机适配器须过 security-policy（Rule of Two）评审后启用 |
| 调度器单点故障 | P2 | 无状态设计（状态可自 runtime/tasks/ 重建）；失败可重启 |

## 五、实现拆解（建议 TASK）

| 建议 TASK | 内容 | 依赖 |
|-----------|------|------|
| TASK-069 | dispatcher 骨架：registry（区分本地/agent 传输条目）+ probe（只读状态收集）+ list/scan 子命令；验收含「远端条目跳过+告警」 | TASK-068 |
| TASK-073 | policy + downlink（本地执行链）+ 单项目一轮端到端：scan→allocate→run→收结果 | TASK-069 |
| TASK-074 | 调度状态机：超时/重试/并发上限/回收 | TASK-073 |
| TASK-075 | 治理挂钩：P0 阻塞、返工上限、告警（monitor → aimonitor） | TASK-074 |
| TASK-076 | 集成验证：2 个真实项目跑通闭环 + 文档（dispatcher README） | TASK-075 |

> 编号注：TASK-070..072 已被 Phase 2 缺口任务占用（远端 agent 上线 / 事件流服务端消费 / 告警闭环），
> 故 Phase 3 剩余任务从 TASK-073 起。

## 六、与 A2A 的关系（远期注记）

调度器的「下行适配器」就是未来 A2A 的挂载点：把 `downlink.py` 的本地 exec 换成 A2A 消息，即可让不同框架的 agent 参与协作。本阶段刻意不做协议化，先用文件系统 + 本地执行链验证协作语义。

## 七、TASK-076 集成验证记录与实现偏差

### 实现偏差（相对 §三 数据流/部署）

| # | 设计稿表述 | 实现（v1） | 影响 |
|---|-----------|-----------|------|
| D-1 | 数据流：探测到 done/in-review → 触发审查 → done | v1 policy 只选 open/in-progress；`in-review → autoloop-reviewer` 分支为**保留分支**，`allocate/run/dispatch` 当前不触发（仅留给未来 policy 扩展） | 无（v1 单机闭环仍完整：open→done 由 coder 链闭环；in-review 仍由各项目 autoloop-reviewer 独立审查） |
| D-2 | dispatch 包含 scan 步骤 | v1 `dispatch` 不输出独立 scan 汇总；`scan` 是独立子命令 | README 已注明 |
| D-3 | 闭环验证建议 aibase（dogfood）+ 下游 | 集成验证用 **westhill + x1design** 两个真实下游项目 | aibase 自身有 open 任务且 TASK-076 处于 in-progress，autoloop-coder 会先捡到本任务自身；**已执行（2026-08-29，TASK-084）**：调度器以 aibase 自身为下行目标跑通 allocate→downlink→本地链（原理由描述如上） |

### 验证记录（2026-08-28）

- **2 项目闭环**：westhill `TASK-020`、x1design `TASK-033`（临时演示任务，P3/P3/fast-path）各跑通
  `dispatch --once`：`task start` + `autoloop-coder --once`（真实本地 agent 会话）→ 任务
  open → in-progress → done，VERIFY 由 `task verify` 真实生成；调度事件轨迹完整
  （allocated → running → done）。演示任务文件已清理，无残留伪任务。
- **治理闸门（真实任务）**：westhill 临时 P0 任务（approval-ref=none）→ `p0-blocked`；
  x1design 临时任务（rework-count=3）→ `rework-rejected`；dry-run 与操作路径均验证，
  `dispatcher.governance-blocked` 事件已记录，未执行任何下行命令。验证用临时任务已清理。
- 验证用注册表子集：`runtime/logs/dispatcher-demo/projects.demo.json`（仅含同机本地条目）。

## 相关

- `docs/ROADMAP.md` — Phase 3 行
- `kit/knowledge/glossary/harness-patterns.md` — Ralph 跨项目扩展原则
- `kit/tools/agent/` — 上行遥测（下行复用其事件流约定）
- `docs/PPT-AIBASE-INTRO-OUTLINE.md` 第 18 页 — 四阶段演进表
