# PHASE3 V2 — 跨机下行适配器设计稿（agent 通道）

> 状态：已由 hb 过目（2026-08-30，会话指令「review 吧」），送独立 REVIEW（TASK-033 in-review）
> 前置：TASK-032（同机试点，验证注册表/闸门在本机可用）；TASK-070（远端 agent 上线，已销账）
> 关联：docs/PHASE3-SCHEDULER-DESIGN.md（v1 设计，本稿为其 v2 演进）

## 一、决策记录（hb 拍板，2026-08-30）

| # | 决策 | 理由 |
|---|------|------|
| Q1 | 主力 x1design 在 hub Linux；dispatcher 统一跑 hub | 单一事实源/全局治理/一张看板；设计稿 §三 中央可跑 aimonitor 机 |
| Q2 | 下行通道 = **agent 消息**（非 SSH） | 部分远端机未开 SSH；agent 通道复用已部署 agent + 现有 token 白名单，**零新增凭据** |
| Q3 | 结果回报 = **agent 事件流** | 与本地事件语义一致（stale 超时回收 / 连败 3 次转人工） |
| — | A2A 记入远期栏，本期不实现 | 设计稿 §六：下行适配器是 A2A 挂载点；先私有插头验证语义 |

## 二、架构（agent 下行通道）

```
中央（hub, dispatcher）
  │ ① HTTP 写指令（POST /downlink-commands，带 project/task/命令/seq）
  ▼
aimonitor server（指令队列 per agent）
  │ ② agent 轮询拾取（poll_interval，默认 30s）
  ▼
远端 agent（win01 等，常驻 install-windows-task.ps1）
  │ ③ 校验：token 白名单项目 + 命令白名单（task/autoloop-*）
  │ ④ 本地执行：task start <id> + autoloop-coder --once（在项目目录内）
  │ ⑤ 结果事件推送（task-events.jsonl / dispatcher-events 增量，带指令 seq）
  ▼
aimonitor server → dispatcher 读回 → state.py 收结果
```

- 沿用 v1 铁律：**中央不直写远端文件系统**，只触发远端本地 CLI。
- 指令需**一次拉取一条 + ack + 幂等**（执行前检查任务状态，与本地 autoloop 锁一致）；分配指纹防双跑。
- 事件带指令 seq（对齐 dispatcher-events.jsonl 单调递增机制）用于乱序/丢包检测。

## 三、消息及时性分析（重点）

### 3.1 时延链路与预算

| 段 | 环节 | 时延 | 量级 |
|----|------|------|------|
| T1 | dispatcher → server 写指令 | <1s | 局域网 HTTP，可忽略 |
| T2 | **agent 拾取指令（轮询）** | ≤ poll_interval（默认 30s；可调 5–10s） | **主要新增开销** |
| T3 | 本地执行 autoloop 单轮 | 分钟级（任务本应有的时延） | **主导项** |
| T4 | 结果事件推送 | 秒级（事件即推；或随下轮轮询批量） | 可忽略 |
| T5 | dispatcher 读回（自己的轮次） | ≤ dispatcher 周期（60s cron；daemon 可更密） | 次要 |

**端到端额外开销（相对本地派发）≈ T2 + T4 + T5 ≈ 30–90s（默认参数）。**

### 3.2 结论：对任务派发足够

- 派发对象是「autoloop 单轮实现任务」= **分钟~小时级**工作；30–90s 的指令/回报开销相对可忽略 → **及时性合格**。
- 需要更快的场景（将来）再降 poll_interval（30→5s，每 agent 每小时请求 120→720 次，aimonitor 轻量 HTTP 无压力；指令负载极小，token/带宽影响可忽略），或上长轮询/WebSocket——**不建议 MVP 就上流式**。

### 3.3 关键风险与对策

| 风险 | 表现 | 对策 |
|------|------|------|
| agent 掉线 | 指令无人拾取 | 指令 ack 超时（建议 60–120s，>2×poll_interval）→ 标记 failed/stale，走 state.py 超时回收；**不要**等心跳阈值 900s（太久） |
| 重复执行 | 重投导致双跑 | 一次拉一条 + 幂等（执行前查任务状态）+ 分配指纹 |
| 乱序/丢包 | 事件 seq 断 | 单调 seq + 重投 + 事件流校验（沿用 v1 seq 机制） |
| 命令注入 | 恶意指令 | agent 侧命令白名单（仅 task/autoloop-*）+ 项目路径白名单（agents.json 既有 per-token 白名单） |
| 拉取负载 | 轮询变密 | 30s 默认足够；5s 也可承受；实时需求出现前不上流式 |

### 3.4 与「半自动现状」的关系

远端 agent 本就在本地跑 autoloop 自主循环；v2 补的是「中央发令」。发令轮询 30s、执行分钟级，与自主循环无冲突——自主循环处理 agent 本地任务池，中央指令处理 dispatcher 分配的任务（两者任务池同一处，靠任务状态 + 分配指纹互斥）。

## 四、安全模型

- 复用现有 agent token 白名单（agents.json，600/gitignored；per-token 项目白名单 = 下行边界）。
- **零新增凭据**（相比 SSH 无需密钥）→ security-policy 红线天然满足；无新密钥入提示词/入库。
- agent 侧校验：命令白名单 + 项目路径白名单 + token 鉴权；失败以 exit code + 事件回报，不静默吞错（沿用 v1 downlink 语义）。

**Rule of Two（Lethal Trifecta）论证**：下行链 agent 同时手握 **①处理不可信输入**（网络下发的指令）+ **③修改状态**（执行白名单 CLI、写任务文件）——此组合为 security-policy 允许项（禁止的是 ②+③→①、①+②→③），但前提是**绝不叠加 ②访问敏感数据**：指令通道、事件、日志均不含密钥/PII，token 只存在于鉴权层（600/gitignored）。①+③ 组合以**两套独立校验**缓解（机械执行，不依赖模型自觉）：服务端 token 鉴权 + per-token 项目白名单；agent 侧命令白名单 + 项目路径白名单。

## 五、实现拆分建议（另行登记 TASK）

1. agent 下行契约扩展（SDD docs：指令格式/ack/seq/幂等/事件回报）——**契约变更，先规格后实现**
2. aimonitor server 指令队列端点（POST /downlink-commands + 拉取端点）
3. agent 轮询拾取 + 校验 + 本地执行 + 回报（install-windows-task.ps1 扩展）
4. dispatcher 侧 agent 适配器（registry transport=agent 不再跳过；probe 经 aimonitor 状态）
5. 集成验证：hub dispatcher → win01 远端项目跑通 allocate→downlink→done

## 六、远期（不实现）

- A2A：把私有下行插头换成 A2A Task/Message，让不同框架 agent 可被派发（设计稿 §六）。
- 流式推送：若出现秒级派发需求，再评估长轮询/WebSocket。
