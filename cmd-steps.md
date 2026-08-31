# cmd-steps — AIOS 框架命令行速查（Linux / Windows）

> 框架 CLI 主要位于 `kit/cli/`；agent/dispatcher 位于 `kit/tools/`。**Python3 命令**：Linux/Windows 原生可跑；**Bash 命令**：
> Linux 原生，Windows 请在 Git Bash / MSYS / WSL 下执行。
>
> ⚠️ `task` 是 python3 脚本，必须 `kit/cli/task ...` 或 `python3 kit/cli/task ...`
> （Windows 用 `python cli\task ...`）。**不要 `bash kit/cli/task`**——会把源码当 shell 解析。

## 一、创建与安装

| 命令 | 平台 | 简介 |
|------|:----:|------|
| `kit/cli/mkproject <目录> [--profile 类型] [--persona 名] [--no-persona] [--from <kit根>]` | Py3 | 用 kit/ 布局创建新项目（可选激活人格） |
| `kit/cli/init [目录] [--profile 类型] [--non-interactive] [--install-deps] [--install-yes]` | Py3 | 把框架安装进已存在项目（幂等，可反复执行） |

## 二、任务生命周期（`kit/cli/task`，Python3）

| 子命令 | 简介 |
|--------|------|
| `new "描述" [--priority] [--risk] [--assignee] [--reviewer] [--parent]` | 新建任务 |
| `list [status]` | 列出任务，可按状态过滤 |
| `show TASK-001` | 查看任务详情 |
| `start TASK-001` | open → in-progress |
| `verify TASK-001` | 真实跑 build/lint/test，通过才生成 VERIFY 记录 |
| `review TASK-001` | in-progress → in-review（独立审查） |
| `approve TASK-001` | 审查通过 |
| `done TASK-001 [--force]` | 标记完成（--force 不得绕过前置条件） |
| `block TASK-001 "原因"` / `unblock TASK-001` | 阻塞（如 P0 等人工批准）/ 解除阻塞 |
| `cancel TASK-001` | 取消任务 |
| `validate` | 校验全部任务文件格式 |
| `index` | 重建任务索引 |
| `stats` / `help` | 统计 / 帮助 |

## 三、框架与沙箱

| 命令 | 平台 | 简介 |
|------|:----:|------|
| `kit/cli/check` | Bash | 框架健康检查（目录/角色/入口/任务格式） |
| `kit/cli/protect [--unlock]` | Bash | 锁 generated_dirs 只读（--unlock 反向解锁） |
| `kit/cli/sandbox-run [--network] -- <命令>` | Bash+Docker | 无网络容器沙箱执行（Rule of Two 隔离） |
| `kit/cli/sandbox-review -- <命令>` | Bash+Docker | 审查专用沙箱（有网+凭据，仅限可信代码） |

## 四、注册监控（agent，Python3）

> 位置：`kit/tools/agent/agent.py`（监控平台 agent 注册链路，TASK-042~044）。
> 配置：`agent.json`（默认读当前目录；示例 `kit/tools/agent/agent.json.example`）。

| 命令 | 简介 |
|------|------|
| `python3 kit/tools/agent/agent.py --register [--config agent.json]` | 注册状态管理（构建申请 → 等待审批 → 已注册） |
| `python3 kit/tools/agent/agent.py --register --status [--config agent.json]` | 查看当前注册状态 |
| `python3 kit/tools/agent/agent.py --register --reset [--config agent.json]` | 重置注册状态（pending → unregistered） |
| `python3 kit/tools/agent/agent.py [--config agent.json] [--once] [--interval N] [--quiet]` | 启动监控推送（默认常驻 30s 轮询；--once 单轮适合 cron/systemd） |
| `python3 kit/tools/agent/agent.py --check-config [--config agent.json]` | 只校验配置（exit 0/1） |

## 五、多项目自动调度（dispatcher，Python3）

> 位置：`kit/tools/dispatcher/dispatcher.py`（Phase 3 中央调度器）。
> 注册表：`projects.json`（默认 `~/code/aimonitor/config/projects.json`，可用 `--config` 覆盖）。
> 调度状态：默认 `<项目根>/runtime/logs/dispatcher`，可用 `--state-dir` 覆盖。

### 常用命令

| 命令 | 简介 |
|------|------|
| `python3 kit/tools/dispatcher/dispatcher.py list --config <projects.json>` | 查看项目注册表 / transport / 可达性 |
| `python3 kit/tools/dispatcher/dispatcher.py scan --config <projects.json>` | 扫描本地项目 `runtime/tasks/` 状态（只读） |
| `python3 kit/tools/dispatcher/dispatcher.py dispatch --once --dry-run --config <projects.json> --max-workers 1` | 调度预演：只报候选与治理判定，不执行命令、不修改状态 |
| `python3 kit/tools/dispatcher/dispatcher.py dispatch --once --config <projects.json> --max-workers 1` | 真正跑一轮调度：open→start+coder；in-progress→coder |
| `python3 kit/tools/dispatcher/dispatcher.py status --config <projects.json> --state-dir <dir> --task-timeout 1800` | 查看调度状态 / 分配 / 超时回收 |
| `python3 kit/tools/dispatcher/dispatcher.py monitor --config <projects.json> --state-dir <dir> --monitor-config <agent.json>` | 推送调度心跳 / 事件 / 治理告警到 aimonitor |
| `python3 kit/tools/dispatcher/dispatcher.py downlink --config <projects.json> --path <项目路径> --command bash --arg kit/cli/check` | 手动在指定项目内执行命令（路径必须属于注册表） |

### 典型操作流程

1. 查看项目注册表：

```bash
python3 kit/tools/dispatcher/dispatcher.py list \
  --config ~/code/aimonitor/config/projects.json
```

2. 扫描各项目任务状态：

```bash
python3 kit/tools/dispatcher/dispatcher.py scan \
  --config ~/code/aimonitor/config/projects.json
```

3. 先做安全预演（推荐）：

```bash
python3 kit/tools/dispatcher/dispatcher.py dispatch --once --dry-run \
  --config ~/code/aimonitor/config/projects.json \
  --max-workers 1
```

4. 真正跑一轮调度：

```bash
python3 kit/tools/dispatcher/dispatcher.py dispatch --once \
  --config ~/code/aimonitor/config/projects.json \
  --max-workers 1
```

5. 查看调度状态：

```bash
python3 kit/tools/dispatcher/dispatcher.py status \
  --config ~/code/aimonitor/config/projects.json \
  --state-dir runtime/logs/dispatcher
```

6. 接入 aimonitor 观测（可选）：

```bash
python3 kit/tools/dispatcher/dispatcher.py monitor \
  --config ~/code/aimonitor/config/projects.json \
  --state-dir runtime/logs/dispatcher \
  --monitor-config ~/code/aimonitor/config/agent.json
```

### 常用参数

| 参数 | 作用 | 默认 |
|------|------|------|
| `--config` | `projects.json` 路径 | `~/code/aimonitor/config/projects.json` |
| `--max-workers` | 全局并发上限 | `1` |
| `--state-dir` | 调度状态 / 事件存放目录 | `<项目根>/runtime/logs/dispatcher` |
| `--task-timeout` | 任务超时秒数，超时标记 stale 可回收 | `1800` |
| `--timeout` | 单条下行命令超时秒数 | `1800` |
| `--rebuild` | 从各项目 `runtime/tasks/` 重建调度状态 | 关闭 |
| `--dry-run` | 只报判定，不执行命令、不修改状态 | 关闭 |

### 当前限制

- 只调度 `transport` 为 `local` 的项目；`agent` 远端项目会跳过并告警
- v1 policy 只选 `open` / `in-progress`；`in-review` 自动审查是保留分支，当前不触发
- 没有 `open` / `in-progress` 任务时，`dispatch` 会提示“无候选任务”
- 治理仍然生效：P0 无 `approval-ref`、`rework-count ≥ 3` 会拦截转人工
- 中央不直接写远端任务文件，只调用目标项目本地 `task` / `autoloop-coder` / `autoloop-reviewer`

## 六、无人值守循环（autoloop，Python3）

| 命令 | 简介 |
|------|------|
| `python kit/cli/autoloop <coder\|reviewer\|both\|stop> [--interval] [--once] [--foreground] [--id] [--timeout]` | 循环启动器（常驻 both 默认后台；--foreground 前台配合 tmux） |
| `python kit/cli/autoloop coder [--interval] [--once] [--unattended] [--id] [--timeout]` | 无人值守 Coder 循环（实验性） |
| `python kit/cli/autoloop reviewer [--interval] [--once] [--unattended] [--id] [--timeout]` | 无人值守 Reviewer 循环（实验性） |

> 兼容 shim（TASK-026）：`python kit/cli/autoloop-coder ...` / `python
> kit/cli/autoloop-reviewer ...` 等价于 `autoloop coder\|reviewer ...`（旧入口名
> 保留，内部已迁 Python）。**不要 `bash kit/cli/autoloop*`**——会把 Python 源码当
> shell 逐行解析（同上文 `task` 的警告）。

- 两循环独立进程，只通过 `runtime/tasks/` 文件耦合（生成者 ≠ 审查者）
- 模型配置：各自启动时设 `ANTHROPIC_MODEL` 环境变量（如 `ANTHROPIC_MODEL=claude-sonnet-4-5`）

## 七、人格与版本发布（Python3）

| 命令 | 简介 |
|------|------|
| `kit/cli/persona list \| use <名> \| off \| show` | 人格按需加载/切换（激活写 `personas/active.md`，未激活=零加载） |
| `kit/cli/persona ensure` | 确保人格已激活：active.md 缺失时从人格库随机激活一个（AI CLI 进入时调用） |
| `kit/cli/publish <共享目录> [--version] [--include <目录>...]` | 冻结当前项目内容打包 + manifest，发布到共享位置 |
| `kit/cli/sync <共享目录> [--package-name] [--version] [--dest]` | 按 manifest 从共享位置拉取指定版本到本地（单向显式同步） |

## 八、自动触发（非手动命令）

| Hook | 作用 |
|------|------|
| `.githooks/pre-commit` | 提交检查：必须有 TASK 引用、不得碰 generated_dirs、check 通过 |
| `.githooks/commit-msg` | 提交信息格式校验 |

生效：`git config core.hooksPath .githooks`（init 安装时自动设置）。

## 提醒

- `--unattended` 会给 claude 传 `--dangerously-skip-permissions`，**仅隔离环境使用**
- `autoloop-*` 依赖 Claude Code CLI；`sandbox-*` 依赖 Docker
- 除 `autoloop-*`（内部调 claude -p）外，其余命令零网络依赖
- `kit/tools/` 含可执行 CLI：`agent/`（注册监控）、`dispatcher/`（Phase 3 调度器）；`browser/filesystem/shell/database/sandbox/git/unity/unreal/docker` 为能力文档；`kit/aios/`、`kit/agents/` 为治理/角色说明，非可执行命令
