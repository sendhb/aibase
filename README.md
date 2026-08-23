# AI Agent Engineering Framework (aibase)

> 面向未来几年的 **AIOS + Reliable AI Engineering Framework**。
>
> 不是"AI 配置模板"，而是 **AI Agent Operating System** —— 定义了 AI 如何**理解目标→建模→规划→执行→验证→学习**的完整工程治理框架。

## 核心创新

| 特性 | 说明 |
|------|------|
| 🧠 **AIOS 内核** | Governance（治理） + Cognition（认知） + Execution（执行引擎） + Context（上下文工程） + Memory（记忆系统） |
| 🤖 **角色式 Agent** | 不再按工具（Claude/Cursor），按角色（Manager/Analyst/Architect/Coder/Reviewer/Tester/Researcher） |
| 🔒 **Governance 协议** | 任务政策 + 修改权限 + 安全红线 + 风险分级 —— AI 不可以做什么，有明确边界 |
| 🔁 **Reliable Execution Loop** | Plan → Impact → Execute → Reflect → Verify → Repair，六环闭环 |
| 🗺️ **知识图谱** | 模块关系 + 依赖图 + 架构决策记录 + 术语表 |
| 📊 **评估体系** | 任务成功率 / 返工次数 / token 消耗 / 模型性能对比 |

## 项目结构（kit 布局）

```
aibase/
├── AGENTS.md            ★ 项目入口（指向 kit/ 内规则）
├── kit/                 ★ 框架（只读模板，与生成的项目结构一致）
│   ├── aios/            AIOS 内核
│   │   ├── governance/    治理协议（任务/修改/安全/风险）
│   │   ├── cognition/     认知层（意图分析/歧义澄清）
│   │   ├── execution/     可靠执行引擎（Plan→Impact→Execute→Reflect→Verify→Repair）
│   │   ├── context/       上下文工程（选择/过滤/压缩/加载）
│   │   ├── memory/        跨会话记忆系统
│   │   ├── policy/        通用原则
│   │   └── protocol/      协议规范
│   ├── agents/          Agent 角色定义（7 个角色）
│   ├── profiles/        项目类型模板（backend/game-server/unity/unreal/frontend/data/design）
│   ├── knowledge/       框架知识库结构
│   ├── tools/           工具能力层（git/filesystem/shell/docker/database/unity/unreal/browser）
│   ├── cli/             统一控制入口（task/init/mkproject/publish/sync/check）
│   ├── evaluation/      质量体系（metrics/benchmarks/failures/reports）
│   └── runtime/         runtime 模板（TASK/VERIFY/REVIEW 模板）
├── runtime/             本仓库运行数据（tasks/states/logs，本地）
├── aios.config.yaml     项目配置
├── install.sh/install.ps1  安装脚本（curl|bash / irm|iex）
└── cmd-steps.md         kit 开发记录
```

> **aibase 自身也是 kit 子目录布局**：`kit/` 结构与 `kit/cli/mkproject` 生成的项目一致，
> 升级 = 整体替换 `kit/` 目录。

## 快速开始

```bash
# 1. 克隆本模板并安装到目标项目（交互式终端会问 profile 类型 + 真实命令，
#    自动生成实例化好的 aios.config.yaml；非交互环境自动退化为纯拷贝模板）
git clone https://github.com/your-org/aibase.git /tmp/framework
bash /tmp/framework/kit/cli/mkproject /path/to/your-project --profile backend

# 2. 锁生成代码目录只读（机械强制，跟具体 AI 工具无关；kit/cli/init 已经跑过一次，
#    generated_dirs 变化后重跑）
bash kit/cli/protect

# 3. 创建第一个任务
bash kit/cli/task new "实现用户登录" --priority P1 --reviewer claude

# 4. 按闭环执行（AI agent 遵循 kit/aios/execution/engine.md）
bash kit/cli/task start TASK-001
bash kit/cli/task verify TASK-001    # 真跑 build/lint/test/check，不是手写记录
bash kit/cli/task review TASK-001
bash kit/cli/task approve TASK-001
```

## Windows 平台使用说明

框架跨平台（Python CLI 层零平台差异），但 bash 命令层需要 Git Bash 或 WSL。要点：

| 场景 | 命令 | 说明 |
|------|------|------|
| 安装 | `irm https://<host>/install.ps1 \| iex` | PowerShell 安装（只检查依赖，不自动装） |
| Python 命令 | `python kit\cli\task ...` | **Windows 用 `python`（不是 `python3`）**；安装 Python 后请确认 `python` 在 PATH |
| bash 命令 | `bash kit/cli/check` 等 | 需 Git Bash 或 WSL（cmd/PowerShell 不能直接跑 bash 脚本） |
| `task verify` | `python kit\cli\task verify TASK-001` | 真实执行 `aios.config.yaml` 的 build/lint/test/check |
| 无人值守 autoloop-* | WSL 下运行 | 依赖 util-linux `flock`，Git Bash/MSYS 不带（会给出明确报错） |
| 沙箱 sandbox-* | 仅 Linux/WSL | 依赖 Docker 容器，Windows 原生不支持 |

其他：脚本/文档已通过 `.gitattributes` 固定行尾（`*.sh`/`*.py`/`*.md` 一律 LF，`*.ps1` 为 CRLF），
Windows 与 Linux 检出确定一致；`install.ps1` 会同时识别 `python3` 与 `python`。

## 设计理念

- **AI 不是"收到命令→执行"的助手**，而是"理解目标→建模→规划→执行→验证→学习"的执行者
- **按角色而非工具定义**：新 AI 工具出现时无需写新适配器，只需确认其 AGENTS.md 支持
- **文件即数据库**：所有协作状态存于文件系统，零依赖、可移植
- **治理先于执行**：任何 AI 动作前先过 governance 协议检查权限/安全/风险

## 署名

- **作者 / Maintainer**：hb <sendhb@21cn.com>

## 许可证

本项目采用 [MIT License](LICENSE) 发布。

Copyright (c) 2026 hb <sendhb@21cn.com>
