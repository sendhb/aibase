# tools/agent

> 见 [tools/README.md](../README.md)

AIOS 通用遥测推送 agent：读取被监控项目 `runtime/` 状态（TASK / 焦点 /
心跳 / 事件 / 验证计数），按固定间隔推送到监控端（aimonitor ingest）。
随 mkproject 自动分发到新项目（TASK-029 集成验证）。

组件分层（自底向上，TASK-022..027）：

| 模块 | 职责 | 任务 |
|------|------|------|
| `agent_config.py` | agent.json 配置加载/校验 | TASK-022 |
| `agent_runtime.py` | 被监控项目 runtime/ 读取 | TASK-023 |
| `agent_payload.py` | 遥测 payload 构造与容量上限 | TASK-024 |
| `agent_http.py` | ingest HTTP 推送客户端 | TASK-025 |
| `agent_retry.py` | 指数退避状态机 | TASK-026 |
| `agent_loop.py` + `agent.py` | 主循环与 CLI 入口 | TASK-027 |

## 配置（agent.json）

示例见 [`agent.json.example`](agent.json.example)——占位符故意不可通过，
防止复制后不填写直接运行：

```json
{
  "server_url": "https://aimonitor.example.com/api/ingest",
  "token": "<your-ingest-token>",
  "projects": [
    {"id": "my-project", "path": "/absolute/path/to/my-project"}
  ],
  "poll_interval_seconds": 30
}
```

| 字段 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `server_url` | ✅ | — | 监控端 ingest 完整地址（http/https，不含 userinfo） |
| `token` | ✅ | — | 推送到监控端的 Bearer token（机密；配置文件建议 `chmod 600`） |
| `projects` | ✅ | — | 被监控项目数组，至少 1 个 |
| `projects[].id` | ✅ | — | 项目唯一标识；多实例部署时在监控端全局唯一（见"多实例 id 约定"） |
| `projects[].path` | ✅ | — | 被监控项目绝对路径（项目根：含 `runtime/` 的目录） |
| `poll_interval_seconds` | — | 30 | 轮询间隔（秒），必须为正数；`--interval` 可临时覆盖 |

校验语义（`agent_config.validate`）：

- 缺失必填字段 / 非法类型 / 非正轮询间隔 → stderr 聚合报错并 **exit 1**；
- 空字符串与 `<...>` 占位符视为未填写（复制 `agent.json.example` 不填就跑会直接报错）；
- 未知多余字段忽略（向前兼容）。

## 使用（CLI）

```bash
python3 agent.py --check-config [--config agent.json]   # 只校验配置（exit 0/1）
python3 agent.py [--config agent.json]                  # 常驻：按 poll_interval_seconds 轮询
python3 agent.py --once                                 # 单轮后退出（cron / systemd timer）
python3 agent.py --interval 10                          # 覆盖轮询间隔（秒）
python3 agent.py --quiet                                # 只输出错误，常规日志静默
```

- `--config` 默认读取当前目录 `./agent.json`；
- SIGINT/SIGTERM → 干净退出（exit 0，无 traceback）；在途 HTTP 请求自然完成；
- 每项目独立退避：A 项目失败退避不阻塞 B 项目推送。

## 部署

### systemd（Linux 常驻，推荐）

`/etc/systemd/system/aios-agent.service`：

```ini
[Unit]
Description=AIOS telemetry agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=myuser
WorkingDirectory=/srv/my-project
ExecStart=/usr/bin/python3 /srv/my-project/kit/tools/agent/agent.py --config /etc/aios/agent.json
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now aios-agent
sudo systemctl status aios-agent    # 查看状态
journalctl -u aios-agent -f         # 查看日志
```

### systemd timer（定时单轮）

用 `--once` 每轮独立推送；失败以 stderr 呈现（退出码恒 0，由日志/告警观察）：

```ini
[Unit]
Description=AIOS telemetry agent (once)
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /srv/my-project/kit/tools/agent/agent.py --once --config /etc/aios/agent.json
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
```

### nohup（无 systemd 环境）

```bash
cd /srv/my-project
nohup python3 kit/tools/agent/agent.py --config /etc/aios/agent.json \
  >> /var/log/aios-agent.log 2>&1 &
echo $! > /var/run/aios-agent.pid
kill "$(cat /var/run/aios-agent.pid)"   # SIGTERM 干净退出
```

日志建议配合 logrotate 轮转；`--once` + cron 可代替常驻（等价 systemd timer）。

### Windows Task Scheduler

任务计划程序 GUI：创建任务 → 触发器（登录/启动/按间隔）→ 操作 → 启动程序 →
`python.exe` + 参数 `C:\srv\my-project\kit\tools\agent\agent.py --config C:\etc\agent.json`。

等价 schtasks 命令（开机启动常驻；建议 `python.exe` 用绝对路径，SYSTEM 账户
PATH 可能与交互用户不同）：

```bat
schtasks /Create /TN "AIOS Agent" /SC ONSTART /RU SYSTEM ^
  /TR "\"C:\Python312\python.exe\" C:\srv\my-project\kit\tools\agent\agent.py --config C:\etc\agent.json"
```

按间隔定时单轮（等价 systemd timer）：`/SC MINUTE /MO 5` + 参数加 `--once`。
所有路径必须用绝对路径；`agent.json` 含 token，注意文件访问权限。

## 与 aimonitor ingest 契约对应

### 请求

| 项 | 值 |
|----|----|
| 方法/地址 | `POST`（完整地址 = `server_url`，示例默认 `/api/ingest`） |
| 请求头 | `Authorization: Bearer <token>`、`Content-Type: application/json`、`User-Agent: aibase-agent/0.1` |
| 请求体 | `serialize_payload()` 输出的紧凑 JSON（UTF-8） |
| 超时 | connect 5s / read 30s（两阶段分离） |

### Payload（AIOS 通用遥测格式）

```json
{
  "project_id": "proj-1",
  "ts": 1786892400.0,
  "files": {
    "tasks": [{"name": "TASK-001.md", "content": "..."}],
    "focus": "...",
    "heartbeats": [{"file": "x.heartbeat", "mtime": 1786892280.0}],
    "events": [{"name": "x-events.jsonl", "content": "..."}],
    "verification_count": 3,
    "review_count": 1
  }
}
```

容量上限：tasks/events/heartbeats 各 ≤ 50 条；单条 content/focus ≤ 4096 字符
（超长截尾 + `…[truncated]` 标记）；整体 ≤ 256 KiB（超出抛 `PayloadTooLargeError`，本轮跳过推送）。

### 状态码（ingest 返回）

| 状态码 | agent 行为 | 可重试 |
|--------|-----------|--------|
| 2xx | 成功（`PushResult`） | — |
| 3xx | 拒绝，不跟随重定向（防 Authorization 异源泄露、防 POST→GET 降级） | 否 |
| 400/401/409/413 及其它 4xx | 拒绝——payload/token/配置需修复 | 否 |
| 429 | 限流——优先按 `Retry-After`（整数秒）退避 | 是 |
| 5xx | 服务端暂时故障 | 是 |
| 网络失败/超时 | 连接被拒 / DNS / 超时 | 是 |

### 重试退避

指数退避 1s→2s→4s→…→32s→60s（cap 60s 恒定）；每项目独立状态；成功 → 计数清零并解除退避；退避中 `can_push()` 跳过本轮，**不阻塞**下一轮轮询。

## 多实例 id 约定

监控端以 `project_id` 为键聚合遥测，因此：

- **全局唯一**：同一监控端下，所有 agent 实例的 `projects[].id` 必须互不相同，
  否则不同项目/主机的遥测互相覆盖混淆。
- **建议格式**：`<hostname>:<project>`（如 `web-01:my-project`）；
  多环境可加环境前缀（如 `prod-web-01:my-project`）。
- **稳定性**：id 一旦发布保持稳定（时间序列按 id 连续聚合），不要用随机值/时间戳。
- **多项目单实例**：一台主机监控多个项目 → 一个 agent.json 配多个 `projects`
  条目，各 id 仍须全局唯一。
- **单项目多实例（HA/负载均衡）**：每个实例用独立 id（如 `my-project-a` /
  `my-project-b` 或主机名后缀），不要共用同一 id。
