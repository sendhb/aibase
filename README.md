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

## 项目结构

```
aibase/
├── AGENTS.md            ★ 所有 AI agent 的统一规则入口
├── README.md            本文
├── aios/                ★ AIOS 内核
│   ├── governance/        治理协议（任务/修改/安全/风险）
│   ├── cognition/         认知层（意图分析/歧义澄清）
│   ├── execution/         可靠执行引擎（Plan→Impact→Execute→Reflect→Verify→Repair）
│   ├── context/           上下文工程（选择/过滤/压缩/加载）
│   ├── memory/            跨会话记忆系统
│   ├── policy/            通用原则
│   └── protocol/          协议规范
├── agents/              ★ Agent 角色定义（7 个角色）
├── profiles/            项目类型模板（backend/game-server/unity/unreal/frontend/data）
├── knowledge/           ★ 知识图谱（模块/依赖/决策/历史/术语）
├── tools/               工具能力层（git/filesystem/shell/docker/database/unity/unreal/browser）
├── runtime/             运行时数据（tasks/states/reviews/verification/logs/memory）
├── evaluation/          质量体系（metrics/benchmarks/failures/reports）
├── cli/                 统一控制入口（init/task/check）
└── docs/                架构文档
```

## 快速开始

```bash
# 1. 克隆本模板并安装到目标项目（交互式终端会问 profile 类型 + 真实命令，
#    自动生成实例化好的 aios.config.yaml；非交互环境自动退化为纯拷贝模板）
git clone https://github.com/your-org/aibase.git /tmp/framework
bash /tmp/framework/cli/init /path/to/your-project

# 2. 锁生成代码目录只读（机械强制，跟具体 AI 工具无关；cli/init 已经跑过一次，
#    generated_dirs 变化后重跑）
bash cli/protect

# 3. 创建第一个任务
bash cli/task new "实现用户登录" --priority P1 --reviewer claude

# 4. 按闭环执行（AI agent 遵循 aios/execution/engine.md）
bash cli/task start TASK-001
bash cli/task verify TASK-001    # 真跑 build/lint/test/check，不是手写记录
bash cli/task review TASK-001
bash cli/task approve TASK-001
```

## 设计理念

- **AI 不是"收到命令→执行"的助手**，而是"理解目标→建模→规划→执行→验证→学习"的执行者
- **按角色而非工具定义**：新 AI 工具出现时无需写新适配器，只需确认其 AGENTS.md 支持
- **文件即数据库**：所有协作状态存于文件系统，零依赖、可移植
- **治理先于执行**：任何 AI 动作前先过 governance 协议检查权限/安全/风险

## 作者

- **sendhb** <sendhb@21cn.com> — 创建者（2026-07）

> 完整作者与贡献者清单见 [AUTHORS](AUTHORS)。


