# 双平台监控部署手册 —— aimonitor + Linux/Windows 双项目

> 目标：从零搭建一套可用的多机监控体系 ——
> **1 台 Linux 服务器**（运行 aimonitor 监控端 + 1 个 Linux 项目实例）+
> **1 台 Windows 机器**（运行 1 个 Windows 项目实例，通过 agent 推送数据）。
>
> 依据（唯一真相）：
> - `aimonitor/docs/MONITOR-SPEC.md` §3.1（Agent 推送模式契约）+ §3.2（注册流程）
> - `aimonitor/docs/OPERATIONS.md`（运维手册）
> - `aibase/kit/tools/agent/README.md`（agent 组件说明，含注册章节）
> - 本手册与实现行为逐项核对（2026-08-21；agent 注册链路 TASK-042~044、分级治理 TASK-047 已落地）。

---

## 0. 拓扑总览

```
┌─────────────────────── Linux 服务器 <monitor-server> ───────────────────────┐
│                                                                          │
│  aimonitor 服务端 (Python 3.12 stdlib, 0.0.0.0:3113)                     │
│    config/projects.json   ← 项目注册（local 直读 + agent 接收）            │
│    config/agents.json     ← agent token 白名单（权限 600, gitignored）    │
│    data/history.db        ← 历史快照（默认保留 90 天）                    │
│    data/ingest.db         ← agent 推送状态 ingest_state（last_seen）      │
│        ▲  GET /api/status /api/history /api/projects/:id/events          │
│        │  POST /api/ingest（Bearer token, 限流, 409 双 agent 冲突检测）   │
│        │                                                                │
│  Linux 项目 <linux-project>（同机，transport: local 直读 runtime/）       │
└──────────────────────────────────────────────────────────────────────────┘
        │ POST /api/ingest（http://<monitor-server>:3113/api/ingest）
        │
┌────────────────────── Windows 机器 <win-host> ──────────────────────┐
│                                                                    │
│  aios-agent（常驻进程，Task Scheduler 启动）                        │
│    读取 C:\srv\<windows-project>\runtime\ 的                        │
│    TASK/focus/heartbeat/events/计数（打包 AIOS 遥测 → 推送）         │
│                                                                    │
│  Windows 项目 <windows-project>（mkproject 生成，kit/ 布局）        │
└────────────────────────────────────────────────────────────────────┘
```

**核心原则：agent 只搬运不解析**。被监控机器上的 agent 只读 `runtime/` 原始文件并打包推送，
解析全在服务端；两种 transport（local 直读 / agent 推送）复用同一套解析逻辑。

---

## 1. 前置准备

### 1.1 软件清单

| 组件 | Linux 服务器（<monitor-server>） | Windows 机器（<win-host>） |
|------|---------------------------|----------------------|
| 操作系统 | Debian/Ubuntu 或兼容 | Windows 10/11 |
| git | ✅ 必需 | ✅ 必需（`winget install Git.Git`） |
| Python | ✅ 3.10+（推荐 3.12，零第三方依赖） | ✅ 3.10+（`winget install Python.Python.3.12`） |
| curl | ✅ 健康检查/验证用 | 可选（Windows 10+ 自带 curl.exe） |
| Docker | 可选（仅 `sandbox-run` 沙箱用，监控链路不需要） | 可选 |
| 网络 | 固定 IP 或域名；对 <win-host> 开放 TCP 3113 | 能访问 <monitor-server>:3113 |

### 1.2 假设

- Linux 服务器 IP：`192.168.1.10`（下文用 `<monitor-server>` 表示）
- Linux 项目：`<linux-project>`，位于 `~/code/<linux-project>`
- Windows 项目：`<windows-project>`，位于 `C:\srv\<windows-project>`
- Windows 机器主机名：`<win-host>`；Linux 机器主机名：`<linux-host>`
- 项目 id（**全局唯一，监控端聚合键**）：`<linux-host>:<linux-project>`、`<win-host>:<windows-project>`

> ⚠️ `project_id` 在同一监控端下必须全局唯一，建议格式 `<hostname>:<project>`。
> 同一 id 只允许一个 agent 推送；两个 agent 抢推同一 id → 服务端返回 **409**。
> id 一旦发布保持稳定（历史趋势按 id 连续聚合），不要用随机值/时间戳。

---

## 2. Phase A：Linux 服务器部署 aimonitor（监控端）

### 2.1 安装框架（可选但推荐）

```bash
# 安装 aibase 框架到 ~/.aibase（只检查 git/python3，不污染系统）
curl -fsSL https://<host>/install.sh | bash        # 或直接 git clone
git clone <aibase-repo-url> ~/aibase               # 等价手动方式
```

### 2.2 获取 aimonitor 代码

```bash
git clone <aimonitor-repo-url> ~/code/aimonitor
cd ~/code/aimonitor
```

### 2.3 注册监控项目：`config/projects.json`

现有文件（local 项目示例）—— **务必保留原有条目**，追加你的两个项目：

```json
{
  "poll_interval_seconds": 30,
  "heartbeat_stale_threshold_seconds": 900,
  "history_retention_days": 90,
  "alert_blocked_ratio_threshold": 0.2,
  "alert_stale_task_days": 14,
  "ingest_rate_limit_per_minute": 60,
  "projects": [
    { "id": "aimonitor", "name": "aimonitor", "path": "~/code/aimonitor" },

    { "id": "<linux-host>:<linux-project>", "name": "<linux-project>",
      "path": "~/code/<linux-project>" },

    { "id": "<win-host>:<windows-project>", "name": "<windows-project>",
      "path": "C:/srv/<windows-project>", "transport": "agent" }
  ]
}
```

字段说明：

| 字段 | 说明 |
|------|------|
| `id` | **实例级唯一**（URL 中使用）。local 项目省略 transport 即默认 local |
| `path` | local 时为服务端本地路径（直接读取）；agent 时仅作展示（真实路径在 agent 侧） |
| `transport` | `local`（默认，服务端直读）/ `agent`（远端 agent 推送） |
| `group` | 可选；同逻辑项目多实例分组（如 `baseline-dev`/`baseline-prod` 同组 `baseline`） |

> 同机 Linux 项目用 `local` 即可（aimonitor 直读其 `runtime/`，无需 agent、无需 token）。

### 2.4 配置 agent token：`config/agents.json`

```bash
cd ~/code/aimonitor
# 生成 token（也可用其它安全随机源）
openssl rand -hex 32
```

创建 `config/agents.json`（**权限必须 600**；含密钥，已 gitignore，禁止入库）：

```json
{
  "<win-host>": {
    "token": "<上面生成的 64 位 hex>",
    "projects": ["<win-host>:<windows-project>"]
  }
}
```

两种格式任选：

| 格式 | 适用场景 | 示例 |
|------|---------|------|
| 扁平 | 每项目独立 token | `{ "<win-host>:<windows-project>": "tok_..." }` |
| agent | 一 token 管多项目（白名单） | `{ "<win-host>": { "token": "tok_...", "projects": ["<win-host>:<windows-project>"] } }` |

```bash
chmod 600 config/agents.json
```

> ⚠️ **fail-closed 语义**：agents.json 缺失 / 权限非 600 / JSON 非法 → 按 `{}` 处理，
> 即**全部 ingest 401**（服务端只服务 local 项目，不半载密钥）。日志会有 ⚠ 提示。
> agents.json 在**服务端启动时加载一次**，修改后需重启服务端。

### 2.5 构建前端并启动

```bash
cd ~/code/aimonitor
bash scripts/start.sh            # 构建 dist/ → 后台启动 → 健康检查
# 输出: ✓ aimonitor 已启动: http://localhost:3113/
```

| 常用选项 | 说明 |
|---------|------|
| `bash scripts/start.sh --port 8080` | 自定义端口 |
| `bash scripts/start.sh --dev` | 开发模式（服务 src/ 源码） |
| `bash scripts/start.sh --no-build` | 跳过前端构建 |
| `bash scripts/stop.sh` | 停止服务 |
| `tail -f runtime/logs/monitor-server.log` | 服务日志 |

### 2.6 验证服务端

```bash
curl -s http://localhost:3113/api/status | python3 -m json.tool | head -40
# 浏览器访问 http://localhost:3113/   （前端仪表盘）
```

`/api/status` 应包含 `<linux-host>:<linux-project>`（local）与 `<win-host>:<windows-project>`（agent，
当前无记录 → `agent 离线` 属正常，agent 上线后自动恢复）。

---

## 3. Phase B：Linux 项目实例（<linux-project>）

### 3.1 创建项目

```bash
# 方式一：mkproject（推荐，kit 布局，自动携带 agent 组件）
bash ~/aibase/kit/cli/mkproject ~/code/<linux-project> --profile backend
cd ~/code/<linux-project>

# 方式二：init（把框架安装到已存在的目录）
python3 ~/aibase/kit/cli/init . --profile backend --non-interactive

# 锁定生成目录只读（可选，治理强制）
bash kit/cli/protect
```

生成结构（kit 布局）：`kit/`（框架只读）+ `knowledge/ docs/ runtime/` + `aios.config.yaml` + `AGENTS.md`。
`kit/tools/agent/` 已随项目分发（零额外安装）。

### 3.2 产生可监控数据（任务 + AI 执行活动）

```bash
cd ~/code/<linux-project>
# 创建任务（产生 runtime/tasks/TASK-001-*.md）
bash kit/cli/task new "实现用户登录" --priority P1

# 启动 autoloop 双角色常驻（coder→reviewer 循环；每轮写心跳文件）
bash kit/cli/autoloop both

# 其他产生数据的活动：
bash kit/cli/task start TASK-001        # 状态流转
bash kit/cli/task verify TASK-001       # 写 VERIFY 记录
bash kit/cli/task review TASK-001       # 写 REVIEW 记录
```

监控端读取的数据来源（`runtime/` 下）：

| 数据 | 位置 | 产生方式 |
|------|------|---------|
| 任务 | `runtime/tasks/TASK-*.md` | `task new / start / ...` |
| 当前焦点 | `runtime/states/CURRENT_FOCUS.md` | 状态维护 |
| 角色心跳 | `runtime/logs/autoloop-{coder,reviewer}.heartbeat` | autoloop 每轮写时间戳 |
| 事件流 | `runtime/logs/autoloop-{coder,reviewer}-events.jsonl` | autoloop 事件记录 |
| 验证/审查计数 | `runtime/verification/` `runtime/reviews/` | `task verify / review` |

### 3.3 同机验证（local 模式）

无需 agent。等 30s（默认轮询间隔）后：

```bash
curl -s http://localhost:3113/api/status | python3 -c "
import json,sys
d=json.load(sys.stdin)
p=next(x for x in d['projects'] if x['id']=='<linux-host>:<linux-project>')
print('tasks:', len(p.get('tasks',[])), '| error:', p.get('error'))"
```

应能看到任务列表，无 error。

---

## 4. Phase C：Windows 项目实例（<windows-project>）

### 4.1 安装框架（Windows）

PowerShell（管理员或用户态均可，`irm | iex` 零安装）：

```powershell
# 方式一：官方安装脚本
irm https://<host>/install.ps1 | iex      # 装到 $HOME\.aibase

# 方式二：手动克隆
git clone <aibase-repo-url> $HOME\aibase
```

依赖检查（脚本只检查不自动装，缺失时）：

```powershell
winget install Git.Git
winget install Python.Python.3.12
# 确认 PATH：新开 PowerShell 后 python --version
```

### 4.2 创建项目

```powershell
python $HOME\.aibase\kit\cli\mkproject C:\srv\<windows-project> --profile backend
cd C:\srv\<windows-project>
```

> Windows 用 `python`（不是 `python3`）；所有路径用绝对路径。
> 生成内容与 Linux 完全一致（kit/ 布局跨平台，纯 Python 标准库无平台差异）。

### 4.3 产生可监控数据

```powershell
cd C:\srv\<windows-project>
python kit\cli\task new "实现 Windows 端数据上报" --priority P1
python kit\cli\task start TASK-001
# 心跳/事件由 autoloop 产生（autoloop 是 bash 脚本，Windows 上用 Git Bash 原生运行；
# 若无，agent 仍可推送任务/焦点/计数数据，心跳字段为 null）
```

### 4.4 配置 agent：`C:\etc\agent.json`（机器级，不在项目内）

**方式 A：预配 token（推荐，当前最简可用）**

```json
{
  "server_url": "http://1.1.1.1:3113/api/ingest",
  "token": "<与 config/agents.json 中 <win-host> 相同的 token>",
  "projects": [
    { "id": "<win-host>:<windows-project>", "path": "C:/srv/<windows-project>" }
  ],
  "poll_interval_seconds": 30
}
```

字段说明（方式 A）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `server_url` | ✅ | ingest 完整地址（http/https，不含 userinfo） |
| `token` | ✅ | 与服务端 `config/agents.json` 对应条目一致（Bearer token） |
| `projects[].id` | ✅ | **必须等于服务端注册的 id**（如 `<win-host>:<windows-project>`） |
| `projects[].path` | ✅ | 被监控项目绝对路径（含 `runtime/` 的目录根） |
| `poll_interval_seconds` | — | 轮询间隔（默认 30） |
| `state` | — | `active`（缺省，兼容存量）/ `unregistered` / `pending`。方式 A 可省略，等同 active |

**方式 B：注册流程（服务端已支持；agent CLI 提交待接通）**

agent 自 v0.2 起支持**自助注册**（详见 `kit/tools/agent/README.md` 注册章节）：

```
state=unregistered → 提交注册申请（POST /api/register）→ state=pending
→ 管理员 dashboard/approve 审批 → 服务端签发 token（写 config/agents.json）
→ agent 轮询到 approved → 领取 token → state=active → 正式推送
```

- 服务端：`POST /api/register`、`/approve|/reject|/revoke` 端点已实现（MONITOR-SPEC §3.2，`monitor_server.py`）；审批通过后**自动签发 token 并写入 agents.json**，无需手动预配。
- agent 侧：`python agent.py --register` 构建注册申请、`--register --status` 查状态、`--register --reset` 重置；pending 轮询领 token 已实现（TASK-043）。
- ⚠️ **已知延迟**：agent CLI `--register` 的 POST /api/register **提交胶水未接通**（TASK-042 ISSUE-02），当前只构建 payload 不实际提交。完整注册链路待接通后，方式 B 即成为零预配推荐路径。

```powershell
# 放好配置后校验（exit 0 = 配置合法）
python C:\srv\<windows-project>\kit\tools\agent\agent.py --check-config --config C:\etc\agent.json

# 单轮试推（产生一次推送；成功应无 401/网络错误输出）
python C:\srv\<windows-project>\kit\tools\agent\agent.py --once --config C:\etc\agent.json
```

### 4.5 部署常驻 agent（Windows Task Scheduler）

```powershell
# 开机自启常驻（SYSTEM 账户；python.exe 用绝对路径）
schtasks /Create /TN "AIOS Agent" /SC ONSTART /RU SYSTEM ^
  /TR "\"C:\Python312\python.exe\" C:\srv\<windows-project>\kit\tools\agent\agent.py --config C:\etc\agent.json"

# 或按间隔定时单轮（等价 systemd timer）：
schtasks /Create /TN "AIOS Agent" /SC MINUTE /MO 5 /RU SYSTEM ^
  /TR "\"C:\Python312\python.exe\" C:\srv\<windows-project>\kit\tools\agent\agent.py --once --config C:\etc\agent.json"

# 立即启动 / 查询 / 停止
schtasks /Run /TN "AIOS Agent"
schtasks /Query /TN "AIOS Agent"
schtasks /End /TN "AIOS Agent"
```

> 也可用任务计划程序 GUI：创建任务 → 触发器（登录/启动/按间隔）→ 操作 → 启动程序 →
> `python.exe` + 参数（同 schtasks）。`agent.json` 含 token，注意 NTFS 文件权限。

### 4.6 Windows agent 的离线/重连语义

- agent 启动后每 30s 推送一轮；推送含 `ts` 与各文件 mtime
- 服务端记录 `last_seen`；`now - last_seen > 900s` → 仪表盘显示该项目 **"agent 离线"**
- 断线重连后**自动恢复**（下次成功推送刷新 last_seen）；断线期间历史快照缺段 = 趋势缺口（不伪造成归零）
- 角色心跳 mtime 变旧（> 900s）→ 该 role `alive=false`，派生 `heartbeat-stale` 告警
  （区分"角色卡死"与"agent 失联"）

---

## 5. 端到端验证清单

| # | 验证项 | 命令 / 位置 | 预期 |
|---|--------|------------|------|
| 1 | 服务端在线 | `curl -s http://localhost:3113/api/status` | 200，`projects` 数组含 3 个项目（按 `id` 过滤） |
| 2 | Linux 项目 local 采集 | status 中 `<linux-host>:<linux-project>` 条目 | tasks 非空，error 为 null |
| 3 | agent 配置合法 | `python kit/tools/agent/agent.py --check-config --config C:\etc\agent.json` | exit 0 |
| 4 | agent 单轮推送 | `... --once --config C:\etc\agent.json` | 无 401/网络错误 |
| 5 | Windows 项目 agent 采集 | status 中 `<win-host>:<windows-project>` 条目 | 无 `"agent 离线"`，tasks 与 Linux 同结构（`transport: "agent"`） |
| 6 | 前端仪表盘 | 浏览器 `http://localhost:3113/` | 三项目可见、心跳存活 |
| 7 | 事件 API | `curl http://localhost:3113/api/projects/<win-host>:<windows-project>/events` | 200 |
| 8 | 历史趋势 | `curl "http://localhost:3113/api/history?hours=1"` | 有快照点 |

---

## 6. 日常使用

### 6.1 项目侧（被监控项目）正常节奏

```bash
# Linux / Git Bash：
bash kit/cli/task new "功能描述" --priority P2
bash kit/cli/task start TASK-001
bash kit/cli/task verify TASK-001     # 真跑 build/lint/test/check
# 分级治理（TASK-047）：
#   P2/P3 且未指定 reviewer → fast-path：verify 通过后直接 task done，无需 review
#   完整路径（P0/P1 或指定 reviewer）→ task review → task approve
bash kit/cli/task done TASK-001       # fast-path
# 或：bash kit/cli/task review TASK-001 && bash kit/cli/task approve TASK-001  # 完整路径
bash kit/cli/autoloop both            # 常驻 coder→reviewer 循环（产生心跳/事件）
```

### 6.2 监控端

```bash
cd ~/code/aimonitor
bash scripts/start.sh                 # 启动
bash scripts/stop.sh                  # 停止
tail -f runtime/logs/monitor-server.log
```

### 6.3 新增一台被监控机器（快速步骤）

1. 服务端 `projects.json` 加条目（`transport: "agent"`）→ 2. `agents.json` 加 token/白名单
   → `chmod 600` → 3. 重启服务端 → 4. 机器上写 `agent.json` → 5. 部署 agent → 6. 验证 status

---

## 7. Token 轮换（顺序：服务端先、agent 后）

```bash
openssl rand -hex 32        # 生成新 token
```

1. **服务端**：更新 `config/agents.json` 对应条目 → `chmod 600` → 重启服务端
   （`bash scripts/stop.sh && bash scripts/start.sh`；重启后旧 token 立即失效）
2. **被监控机器**：更新 `agent.json` 的 `token` → 重启 agent
   （Task Scheduler：`schtasks /End` + `/Run`；systemd：`systemctl restart aios-agent`）
3. **验证**：`agent.py --check-config` → `agent.py --once` 无 401 → 服务端 status 的
   `last_seen` 刷新、无 "agent 离线"

> 4xx 对 agent 是**不可重试**错误：若先改 agent 侧，agent 会持续 401 直到服务端对齐。
> 多 agent 逐个轮换，避免同时中断全部远程项目。

---

## 8. Agent 离线排查速查

| 症状 | 含义 | 判定 |
|------|------|------|
| 项目 `error = "agent 离线"` | agent 整体失联（进程死/网络断/token 失效/从未推送） | `last_seen` 超阈值（900s）或无记录 |
| `heartbeat-stale` 告警 | 远端角色进程卡死，但 agent 在线 | 心跳 mtime 变旧 |
| agent 日志持续 401 | token 与服务端不一致 | 按 §7 对齐 |
| 服务端 429 | 超限流（默认 60/min） | 降频或调大；agent 自动退避 |
| 连接被拒/超时 | 网络/防火墙/服务端未启动 | 确认 `0.0.0.0:3113` 监听、防火墙放行 |

自底向上：agent 进程 → agent 日志 → 网络可达 → `--check-config` → 服务端日志/agents.json 权限。

---

## 9. 安全注意事项

- `config/agents.json`（服务端）与 `agent.json`（被监控机器）**含密钥**：权限 `600`、
  已 gitignore、禁止入库/入提示词/入日志
- 服务端 agents.json 权限非 600 → **fail-closed 全部 401**（宁可不可用，不半载密钥）
- 401 不泄露任何状态数据（不区分"缺失"与"错误"token）
- 同一 `project_id` 双 agent → 409（防跨 agent 覆盖污染）
- 跨公网部署建议服务端前置 **HTTPS 反向代理**（Nginx/Caddy），agent 的 `server_url` 用 https
- agent HTTP 客户端**不跟随 3xx 重定向**（防 Authorization 异源泄露、防 POST→GET 降级）

---

## 10. FAQ

**Q1：Windows 项目一定要 agent 吗？**
是的。aimonitor 只读本机文件系统（local），无法访问 Windows 的 `runtime/`。Windows 项目
必须 `transport: "agent"`，由本机 agent 推送。

**Q2：Linux 项目可以也走 agent 吗？**
可以。若 Linux 项目在另一台机器（或与 aimonitor 不同机），同样注册 `transport: "agent"`
并部署 agent。本手册中同机 Linux 项目用 local 是"零部署"的最简方案。

**Q3：一台 Windows 机器有多个项目怎么办？**
一台机器跑 **1 个 agent 实例**，`agent.json` 的 `projects` 数组配多个条目（id 全局唯一），
服务端 `agents.json` 用 agent 格式给该 token 配多个项目白名单。

**Q4：同一个逻辑项目部署在 Linux 和 Windows 两台机器？**
两个实例 = 两个独立监控单元，id 各自唯一（如 `<linux-host>:app`、`<win-host>:app`），可用
`group` 字段分组展示。

**Q5：agent 推送的数据量上限？**
tasks/events/heartbeats 各 ≤ 50 条；单条 content ≤ 4096 字符（超长截尾 + 标记）；
整体 ≤ 256 KiB（超出本轮跳过）。服务端 ingest 上限 5 MB。

**Q6：agent 需要 Docker 吗？**
不需要。agent 是纯 Python 标准库。Docker 只与项目侧的 `sandbox-run` 沙箱功能相关。

**Q7：Windows 上 autoloop 心跳不产生，会影响监控吗？**
不影响监控链路。心跳/事件缺失时对应字段为 null，任务/焦点/计数照常推送；
autoloop 可在 Git Bash 下原生运行以产生心跳与事件流（TASK-012 起无需 WSL/flock）。

**Q8：改 agents.json 后要重启吗？**
要。服务端启动时加载一次 agents.json；projects.json 的轮询配置同理（修改后重启生效）。

---

## 附录 A：三份配置文件完整示例（最小可运行）

**`aimonitor/config/projects.json`**（追加两项目）

```json
{
  "poll_interval_seconds": 30,
  "heartbeat_stale_threshold_seconds": 900,
  "history_retention_days": 90,
  "ingest_rate_limit_per_minute": 60,
  "projects": [
    { "id": "aimonitor", "name": "aimonitor", "path": "~/code/aimonitor" },
    { "id": "<linux-host>:<linux-project>", "name": "<linux-project>", "path": "~/code/<linux-project>" },
    { "id": "<win-host>:<windows-project>", "name": "<windows-project>", "path": "C:/srv/<windows-project>", "transport": "agent" }
  ]
}
```

**`aimonitor/config/agents.json`**（权限 600）

```json
{
  "<win-host>": {
    "token": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "projects": ["<win-host>:<windows-project>"]
  }
}
```

**`C:\etc\agent.json`**（Windows 机器）

```json
{
  "server_url": "http://localhost:3113/api/ingest",
  "token": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "projects": [ { "id": "<win-host>:<windows-project>", "path": "C:/srv/<windows-project>" } ],
  "poll_interval_seconds": 30,
  "state": "active"
}
```

> `state` 缺省 `active` 兼容存量；注册流程（方式 B）时可为 `unregistered`/`pending`（见 §4.4）。

## 附录 B：命令速查

```bash
# ── 监控端（Linux）──
bash scripts/start.sh                          # 构建 + 启动 + 健康检查
bash scripts/stop.sh                           # 停止
tail -f runtime/logs/monitor-server.log        # 日志
curl -s http://localhost:3113/api/status       # 聚合状态
openssl rand -hex 32                           # 生成 token

# ── 项目侧（Linux / Git Bash）──
bash kit/cli/mkproject ~/code/<linux-project> --profile backend   # 创建项目（Python 跨平台）
bash kit/cli/task new "描述" --priority P1
bash kit/cli/autoloop both

# ── agent（Windows PowerShell）──
python kit\tools\agent\agent.py --check-config --config C:\etc\agent.json
python kit\tools\agent\agent.py --once --config C:\etc\agent.json
schtasks /Run /TN "AIOS Agent"
```
