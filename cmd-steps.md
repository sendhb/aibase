# cmd-steps — AIOS 框架命令行速查（Linux / Windows）

> 全部命令位于 `kit/cli/`。**Python3 命令**：Linux/Windows 原生可跑；**Bash 命令**：
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

## 五、无人值守循环（autoloop，Bash）

| 命令 | 简介 |
|------|------|
| `kit/cli/autoloop <coder\|reviewer\|both\|stop> [--interval] [--once] [--foreground] [--id] [--timeout]` | 循环启动器（常驻 both 默认后台；--foreground 前台配合 tmux） |
| `kit/cli/autoloop-coder [--interval] [--once] [--unattended] [--id] [--timeout]` | 无人值守 Coder 循环（实验性） |
| `kit/cli/autoloop-reviewer [--interval] [--once] [--unattended] [--id] [--timeout]` | 无人值守 Reviewer 循环（实验性） |

- 两循环独立进程，只通过 `runtime/tasks/` 文件耦合（生成者 ≠ 审查者）
- 模型配置：各自启动时设 `ANTHROPIC_MODEL` 环境变量（如 `ANTHROPIC_MODEL=claude-sonnet-4-5`）

## 六、人格与版本发布（Python3）

| 命令 | 简介 |
|------|------|
| `kit/cli/persona list \| use <名> \| off \| show` | 人格按需加载/切换（激活写 `personas/active.md`，未激活=零加载） |
| `kit/cli/persona ensure` | 确保人格已激活：active.md 缺失时从人格库随机激活一个（AI CLI 进入时调用） |
| `kit/cli/publish <共享目录> [--version] [--include <目录>...]` | 冻结当前项目内容打包 + manifest，发布到共享位置 |
| `kit/cli/sync <共享目录> [--package-name] [--version] [--dest]` | 按 manifest 从共享位置拉取指定版本到本地（单向显式同步） |

## 七、自动触发（非手动命令）

| Hook | 作用 |
|------|------|
| `.githooks/pre-commit` | 提交检查：必须有 TASK 引用、不得碰 generated_dirs、check 通过 |
| `.githooks/commit-msg` | 提交信息格式校验 |

生效：`git config core.hooksPath .githooks`（init 安装时自动设置）。

## 提醒

- `--unattended` 会给 claude 传 `--dangerously-skip-permissions`，**仅隔离环境使用**
- `autoloop-*` 依赖 Claude Code CLI；`sandbox-*` 依赖 Docker
- 除 `autoloop-*`（内部调 claude -p）外，其余命令零网络依赖
- `kit/tools/` 仅 `agent/` 含可执行 CLI（注册监控）；`browser/filesystem/shell/database/sandbox/git/unity/unreal/docker` 为能力文档；`kit/aios/`、`kit/agents/` 为治理/角色说明，非可执行命令
