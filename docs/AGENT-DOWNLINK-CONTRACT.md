# AGENT-DOWNLINK-CONTRACT — 下行指令契约（v1.0）

> TASK-034 交付物｜上游设计：`docs/PHASE3-V2-CROSSMACHINE-DESIGN.md`（§二/§三/§四）
> 状态：送审（in-review）｜本契约是 TASK-035/036/037 的唯一规格源，实现与契约冲突时以本契约为准，改契约须走 REVIEW。

## 一、角色与端点总览

| 角色 | 端点 | 鉴权 |
|------|------|------|
| dispatcher（中央） | `POST /api/downlink/commands` 写入指令；`GET /api/downlink/commands/{id}` 轮询状态 | Bearer token（dispatcher 自身注册 token） |
| agent（远端） | `GET /api/downlink/pickup` 拾取；`POST /api/downlink/commands/{id}/result` 回报 | Bearer token（agent 注册 token） |

- 鉴权复用现有 Bearer token 机制（同 /api/ingest）；**零新增凭据类型**。
- server 双闸：写入侧校验 project ∈ 注册表且为 agent 传输条目；拾取侧只下发**该 token 白名单内项目**的指令。

## 二、指令对象（Command Object）字段级 schema

```json
{
  "command_id": "dl-000042",          // server 分配，队列内单调递增
  "seq": 42,                           // 同 command_id，单调递增，用于乱序/重放检测
  "dedup_key": "x1prototype:TASK-009:coder",  // dispatcher 提供，幂等键 = project:task:role
  "project_id": "x1prototype",
  "command": {
    "name": "task_start | autoloop_coder | autoloop_reviewer",   // 白名单枚举，仅此三值
    "args": ["TASK-009"]               // 字符串数组；agent 拼接为本地 CLI 参数
  },
  "timeout_secs": 1800,                // 执行超时（与 v1 downlink 默认一致）
  "created_by": "dispatcher",          // 写入方标识
  "created_at": "2026-08-30T12:00:00Z"
}
```

- `command.name` **白名单**：`task_start` / `autoloop_coder` / `autoloop_reviewer`。映射本地 CLI：
  `task_start` → `python kit/cli/task start <args>`；`autoloop_coder` → `python kit/cli/autoloop coder --once <args>`；`autoloop_reviewer` → `python kit/cli/autoloop reviewer --once <args>`。
- 白名单外 `name` → server 400 拒绝（第一道闸）；agent 侧再校验（第二道闸，独立实现）。

## 三、指令状态机（含 R2-001 并发模型）

```
queued ──pickup──▶ running ──result──▶ done | failed | skipped
   │                                    ▲
   ├─ pickup 超时（默认 90s > 2×poll_interval）─▶ stale → 重投（≤2 次）─▶ failed(human)
   └─ 执行超时（timeout_secs，默认 1800s）────▶ stale → 转人工（与本地 state.py 语义一致）
```

**R2-001 并发模型（强制实现约束）**：
1. agent 的**拾取循环独立于执行**——拾取是轻量 HTTP 轮询（每 poll_interval 一次），执行是子进程；执行期间拾取循环照常运行。
2. 指令一经 pickup 即 `running`，**pickup 超时计时终止**；此后只有 `timeout_secs` 执行超时生效——「忙而非死」的 agent 不会被误判 stale。
3. 幂等三层：① server 端 `dedup_key` 去重（同 key 未终态的指令拒绝重复入队 409）；② agent 执行前查本地任务状态（已 done/in-review → 回报 `skipped`）；③ 分配指纹（dispatcher state.py）防双派。

## 四、结果回报（Result Report）schema

```json
{
  "command_id": "dl-000042",
  "status": "done | failed | skipped",
  "exit_code": 0,
  "stdout_tail": "…≤200 行…",
  "stderr_tail": "…≤200 行…",
  "finished_at": "2026-08-30T12:25:00Z"
}
```

- tail 截断 ≤200 行；回报前**脱敏**：剔除含 `Authorization`/`token`/`Bearer` 字样的行（防密钥入日志）。
- server 收到回报：指令终态落库 + 追加事件到该项目事件流（沿用现有事件机制，dispatcher 经 GET /api/projects/{id}/events 或状态轮询读回）。

## 五、错误与超时语义

| 场景 | 行为 |
|------|------|
| 写入：project 非 agent 条目 / 不在注册表 | 400，不入队 |
| 写入：dedup_key 重复且有未终态指令 | 409，返回既有 command_id |
| 拾取：白名单外项目指令 | server 不下发（等价于队列为空） |
| 拾取：无 token / 无效 token | 401 |
| pickup 超时（默认 90s） | 标 stale → 重投 ≤2 次 → failed(human)，事件 `downlink.stale` |
| 执行超时（timeout_secs） | agent 子进程 kill → 回报 failed(exit_code=-1)；dispatcher 侧 state.py 亦按本地语义回收 |
| agent 掉线（无心跳） | 指令滞留 queued → pickup 超时路径兜底 |
| seq 重放/乱序（R2-002 负例） | server 端 seq 单调校验；重复 command_id 的 result → 409 幂等忽略 |

## 六、安全映射（对照 security-policy / 设计稿 §四）

- Rule of Two：agent 手握 ①不可信输入（网络指令）+ ③修改状态（执行 CLI）= 允许组合，②敏感数据不入通道/日志（本契约 §四脱敏 + §二白名单双闸）。
- 双套独立校验：server 侧（token + 项目注册表 + 命令白名单 400 闸）与 agent 侧（token + per-token 项目白名单 + 命令白名单）**各自独立实现**，不共享代码路径。
- token：600 权限、gitignored、仅存在于鉴权层，绝不入 payload/日志/提示词。

## 七、版本与演进

- 契约版本号：本文档头部 `v1.0`；不兼容变更必须升版本 + REVIEW。
- A2A 挂载点：`command.name` 白名单枚举即未来 A2A Task 类型的映射点（远期）。
