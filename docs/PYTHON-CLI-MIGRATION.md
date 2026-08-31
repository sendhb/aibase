# Python CLI 迁移任务（TASK-085 ~ TASK-093）

> 目标：把 `autoloop` 和 `task` 命令统一为 **Python 一套实现**，消除 Bash + Python 双语言维护。
> 本文件是 **tracked 任务定义**，不是 `runtime/tasks/` 里的 TASK 文件。
> 各机器根据本文件在本地创建 TASK 后执行，避免把 `runtime/tasks/` 提交进 git。

## 为什么用 tracked 文档而不是直接 commit runtime/tasks

- `runtime/tasks/TASK-*.md` 已被 `.gitignore` 忽略，属于本地运行时状态
- 同一项目多实例（Linux / Windows）需要共享任务定义，但不能共享运行状态
- 正确方式：commit 任务定义文档，各机器本地 `task new` 生成 TASK

## 在 Windows 上使用

1. Linux 侧 commit 并 push 本文件
2. Windows 侧 `git pull`
3. Windows 侧按本文档创建本地 TASK，例如：

```powershell
cd D:\share\the5\aibase

python kit\cli\task new "TASK-085 迁移设计与兼容矩阵" --priority P1 --risk P1
```

4. 本地执行：

```powershell
# Git Bash / WSL（当前 autoloop 仍是 Bash）
bash kit/cli/autoloop both --once
```

> 等迁移完成后，Windows 可直接 `python kit\cli\autoloop ...`。

---

## TASK-085 — 迁移设计与兼容矩阵

- **优先级/风险**：P1 / P1
- **依赖**：无
- **目标**：确定迁移范围、兼容红线、旧入口和新入口对照。
- **范围**：
  - 新增 `docs/CLI-PYTHON-MIGRATION.md` 或复用本文档
  - 列出旧入口：`autoloop`、`autoloop-coder`、`autoloop-reviewer`、`task`
  - 列出必须保持兼容的格式：TASK frontmatter、VERIFY/REVIEW、heartbeat、events.jsonl、lock 文件
  - 列出所有调用点：README、cmd-steps、docs、dispatcher、tests
- **验收标准**：
  - [x] 文档包含“旧命令 → 新命令”对照表
  - [x] 文档明确保留哪些兼容 shim、删除哪些旧脚本
  - [x] 文档列明 dispatcher 和监控端无感知的兼容红线

---

### TASK-085 产出：迁移设计与兼容矩阵（2026-08-30 落地）

#### 1. 现状盘点（2026-08-30 实测）

| 入口 | 当前实现 | 位置 | 说明 |
|------|---------|------|------|
| `task` | **已是 Python 3** | `kit/cli/task`（33.8KB） | 任务 CLI，本仓库自举版；重构时抽 `tasklib` 复用 |
| `autoloop` | **Python 3**（TASK-025 重写） | `kit/cli/autoloop` + `lib/autoloop_launcher.py` | 启动器：coder/reviewer/both/stop、--once/--interval/--foreground/--coder-llm/--reviewer-llm |
| `autoloop-coder` | **Python 3**（薄 shim，TASK-026） | `kit/cli/autoloop-coder` → `lib/autoloop_coder.py` | 无人值守 Coder 循环（选任务/P0 block/rework/fast-path/verify/review） |
| `autoloop-reviewer` | **Python 3**（薄 shim，TASK-026） | `kit/cli/autoloop-reviewer` → `lib/autoloop_reviewer.py` | 无人值守 Reviewer 循环（生成者≠审查者、approve/打回/block） |

> 2026-08-30 更新（TASK-026 落地）：三个入口全部 Python 化；旧 Bash 实现已由 shim 取代
>（历史版本在 git 历史中），`bash kit/cli/autoloop*` 调用不再有效，文档已改为新命令。

Bash 依赖（`kit/cli/lib/`）：`heartbeat.sh`、`llm.sh`、`lock_guard.sh`、`pick_python.sh`
（TASK-026 后入口不再使用，待 TASK-093 清理）；
已是 Python 的：`config.py`、`init_wizard.py`、`lock.py`、`precommit_check.py`。

#### 2. 旧命令 → 新命令对照表

| 旧（Bash） | 新（Python） | 迁移后状态 |
|------------|-------------|-----------|
| `bash kit/cli/autoloop-coder --once` | `python3 kit/cli/autoloop coder --once` | ✅ 已保留 `autoloop-coder` 为 Python 薄 shim（TASK-026） |
| `bash kit/cli/autoloop-reviewer --once` | `python3 kit/cli/autoloop reviewer --once` | ✅ 已保留 `autoloop-reviewer` 为 Python 薄 shim（TASK-026） |
| `bash kit/cli/autoloop both` | `python3 kit/cli/autoloop both` | ✅ 已重写 `kit/cli/autoloop` 为 Python（TASK-025） |
| `bash kit/cli/autoloop stop` | `python3 kit/cli/autoloop stop` | 同上 |
| `python3 kit/cli/task ...` | 不变 | 重构内部用 `tasklib`（TASK-086），CLI 对外契约不变 |

#### 3. 必须保持兼容的格式（红线）

| 格式 | 位置 | 消费者 | 红线 |
|------|------|--------|------|
| TASK frontmatter（YAML） | `runtime/tasks/TASK-*.md` | `task` CLI / aimonitor 解析 | 字段名/语义不变：id、title、status、priority、risk、assignee、reviewer、approval-ref、rework-count、metadata |
| VERIFY/REVIEW 记录 | 任务文件正文章节 + `runtime/verification/` | reviewer、监控端 | 生成位置与摘要格式不变（`VERIFY-<date>-<task>`、`REVIEW-<date>-<task>`） |
| heartbeat | `runtime/logs/autoloop-{coder,reviewer}.heartbeat` | aimonitor `heartbeat-stale` 告警、dispatcher `monitor.py` | 每轮循环开始写时间戳，**mtime 语义不变** |
| events.jsonl | `runtime/logs/autoloop-*-events.jsonl` | aimonitor TASK-071 ingest | 每行一个 JSON `{ts, task, outcome}`，**append-only 行格式不变** |
| lock 文件 | `runtime/locks/autoloop-{coder,reviewer}.lock` | 并发防重 | 继续用 `lib/lock.py`（msvcrt/fcntl 跨平台），PID 文件语义不变 |
| 状态机 | `runtime/tasks/` | task CLI、dispatcher policy | open→in-progress→in-review→done；`rework-count ≥ 3` 转人工；P0 无 approval-ref 转 blocked |

#### 4. 调用点清单（TASK-091 需更新）

- 文档：`cmd-steps.md`、`docs/DEPLOY-DUAL-PLATFORM.md`（215/448/601）、`docs/PHASE3-SCHEDULER-DESIGN.md`、`docs/PPT-AIBASE-INTRO-OUTLINE.md`、`docs/ppt/build_aibase_ppt.py`、`kit/cli/autoloop-boot.md`
- 治理文档：`kit/aios/execution/engine.md`（186）、`kit/aios/governance/task-policy.md`（75）
- dispatcher：`kit/tools/dispatcher/dispatcher.py`（223/233 行调用 `bash kit/cli/autoloop-reviewer|autoloop-coder --once`）、`kit/tools/dispatcher/README.md`、`downlink.py`、`monitor.py`（heartbeat 语义）、`policy.py`
- 测试：`kit/tests/`（dispatcher 测试内嵌假 `autoloop-coder` shell 脚本）

#### 5. dispatcher 与监控端无感知红线

1. **dispatcher 下行链**：`bash kit/cli/autoloop-coder --once` / `bash kit/cli/autoloop-reviewer --once` 在迁移后**仍可执行**（薄 shim 保证），或同步改 dispatcher.py 为 `python3 kit/cli/autoloop coder --once`；两种方式二选一，不得留下不可执行引用。
2. **aimonitor 服务端无感知**：heartbeat 文件 mtime、events.jsonl 行格式、TASK frontmatter 解析结果不变 → 服务端（ingest/告警/趋势）零改动。
3. **`task` CLI 对外契约不变**：`tasklib` 抽取只改内部实现；`task start/block/review/verify` 输出与状态机行为与现状一致。
4. **LLM 密钥安全**：`llm.py` 沿袭 `llm.sh` 的 Rule of Two —— deepseek key 只从 `~/.pi/agent/models.json` 或环境变量读取，**不写代码、不打印**（security-policy）。

---

## TASK-086 — 抽取共享 Python 模块 `tasklib.py`

- **优先级/风险**：P1 / P1
- **依赖**：TASK-085
- **目标**：把 `task` 里可复用的任务读写、状态机、治理判断抽出来，供 autoloop import。
- **范围**：
  - 新增 `kit/cli/lib/tasklib.py`
  - 迁移：项目根、TASK 解析、任务查找、`set_status`、`append_event`、VERIFY/REVIEW 校验、fast-path 判断、P0/rework 治理、assignee 写入
  - 重构 `kit/cli/task` 使用 `tasklib`
- **验收标准**：
  - [ ] `python3 -m py_compile kit/cli/task kit/cli/lib/tasklib.py` 通过
  - [ ] 现有 `kit/tests/test_task_*.py` 全部通过
  - [ ] autoloop 可通过 import 使用任务解析，不再用 shell grep/awk/sed

---

## TASK-087 — LLM 调用与事件/日志 Python 化

- **优先级/风险**：P1 / P1
- **依赖**：TASK-086
- **目标**：把 `lib/llm.sh`、`heartbeat.sh` 的能力迁到 Python。
- **范围**：
  - 新增 `kit/cli/lib/llm.py`：封装 `pi` / `claude` / `deepseek`
  - 新增 `kit/cli/lib/events.py`：heartbeat、autoloop 事件 JSONL
  - 用 `subprocess.run(..., timeout=...)` 替代 shell `timeout`
- **验收标准**：
  - [ ] `run_llm("pi", prompt, timeout=10)` 行为与旧 `llm.sh pi` 一致
  - [ ] 心跳文件和 `autoloop-*-events.jsonl` 格式与旧版一致
  - [ ] LLM 超时时能杀子进程并记录 `timeout` 事件

---

## TASK-088 — Python 版 `autoloop-coder`

- **优先级/风险**：P1 / P1
- **依赖**：TASK-086、TASK-087
- **目标**：把 `autoloop-coder` 从 Bash 迁成 Python。
- **范围**：
  - 新增 `kit/cli/autoloop` 的 `coder` 子命令，或重写 `kit/cli/autoloop-coder` 为 Python
  - 保持：选任务、P0 block、rework block、fast-path、assignee、prompt、verify/review/done、心跳事件
- **验收标准**：
  - [ ] `python3 kit/cli/autoloop coder --once` 跑通一个 P2 fast-path 任务
  - [ ] 与旧 `bash kit/cli/autoloop-coder --once` 状态变化和事件日志一致
  - [ ] P0 无批准任务转 blocked

---

## TASK-089 — Python 版 `autoloop-reviewer`

- **优先级/风险**：P1 / P1
- **依赖**：TASK-086、TASK-087
- **目标**：把 `autoloop-reviewer` 从 Bash 迁成 Python。
- **范围**：
  - 新增 `kit/cli/autoloop` 的 `reviewer` 子命令，或重写 `autoloop-reviewer` 为 Python
  - 保持：选 in-review 且 assignee ≠ 自己、fast-path 跳过、prompt、approve/打回/block、心跳事件
- **验收标准**：
  - [ ] `python3 kit/cli/autoloop reviewer --once` 能审查完整路径任务
  - [ ] 生成者 ≠ 审查者逻辑仍然生效
  - [ ] P0 缺 approval-ref 时 block

---

## TASK-090 — Python 版 `autoloop` 启动器

- **优先级/风险**：P1 / P1
- **依赖**：TASK-088、TASK-089
- **目标**：把 `autoloop` 的 `coder/reviewer/both/stop` 启动逻辑迁到 Python。
- **范围**：
  - 重写 `kit/cli/autoloop` 为 Python
  - 支持：`coder` / `reviewer` / `both` / `stop`、`--once`、`--interval`、`--foreground`、`--coder-llm`、`--reviewer-llm`
  - 后台化用 Python `Popen` / `start_new_session`
  - 继续使用 `lib/lock.py`，保留 PID 文件
- **验收标准**：
  - [ ] `autoloop both --once` 顺序执行 coder → reviewer
  - [ ] `autoloop both` 默认后台启动，日志和 PID 文件正确
  - [ ] `autoloop stop` 能停止常驻实例

---

## TASK-091 — 兼容 shim、文档与调用点更新

- **优先级/风险**：P2 / P2
- **依赖**：TASK-090
- **目标**：让旧命令和文档平滑过渡。
- **范围**：
  - 保留 `autoloop-coder` / `autoloop-reviewer` 为 Python 薄入口
  - 更新 `cmd-steps.md`、`README.md`、`docs/PHASE3-SCHEDULER-DESIGN.md`、`docs/DEPLOY-DUAL-PLATFORM.md`、PPT 脚本
- **验收标准**：
  - [ ] 旧命令 `bash kit/cli/autoloop-coder --once` 仍可用（或文档明确改为新命令）
  - [ ] 新命令 `python3 kit/cli/autoloop coder --once` 可用
  - [ ] 全仓 grep 不再出现“autoloop 是 Bash”的过时说明

---

### TASK-091 产出（TASK-026 落地，2026-08-30）

- `kit/cli/autoloop-coder` / `kit/cli/autoloop-reviewer` 重写为 Python 薄 shim：参数原样透传给
  `python kit/cli/autoloop coder|reviewer ...`（与 `kit/cli/autoloop` 同模式：sys.path 接线 + launcher main）
- dispatcher 下行链改为 `[sys.executable, kit/cli/autoloop-coder|autoloop-reviewer, --once]`
  ——不留不可执行引用；`kit/tests/test_dispatcher_{downlink,governance}.py` 内嵌假脚本同步改 Python
- 文档调用点更新：`cmd-steps.md`、`README.md`、`kit/cli/README.md`、`docs/PHASE3-SCHEDULER-DESIGN.md`、
  `docs/DEPLOY-DUAL-PLATFORM.md`、`docs/PPT-AIBASE-INTRO-OUTLINE.md`、`docs/ppt/build_aibase_ppt.py`
- 验收标准第一项取「或」分支：旧命令 `bash kit/cli/autoloop-coder --once` 随 Bash 实现一并移除
  （Python 源码不可被 bash 执行，强留会死循环），文档已明确改为新命令

## TASK-092 — 测试补齐与回归

- **优先级/风险**：P1 / P1
- **依赖**：TASK-088 ~ TASK-091
- **目标**：确保迁移不破坏现有功能。
- **范围**：
  - 新增 `test_tasklib.py`、`test_llm.py`、`test_autoloop_python.py`、`test_autoloop_both.py`
  - 适配 dispatcher 测试中假的 `autoloop-coder` shell 脚本
  - 跑全量测试
- **验收标准**：
  - [ ] `python3 -m pytest kit/tests -q` 全部通过
  - [ ] dispatcher 下行调用 `autoloop-coder --once` 的测试仍然通过
  - [ ] 真实项目 `autoloop both --once` 闭环成功

---

## TASK-093 — 清理旧 Bash 实现（可选）

- **优先级/风险**：P3 / P2
- **依赖**：TASK-092
- **目标**：迁移稳定后删除冗余 Bash 代码。
- **范围**：
  - 删除 Bash 版 `autoloop-coder` / `autoloop-reviewer`
  - 删除不再使用的 `lib/heartbeat.sh`、`lib/lock_guard.sh`、`lib/llm.sh`
  - 保留 `lib/pick_python.sh`（其他 Bash 命令仍可能使用）
- **验收标准**：
  - [ ] `grep -R "autoloop-coder" docs kit/tests | grep bash` 无残留
  - [ ] `kit/cli/check` 仍通过
  - [ ] 新生成项目里 autoloop 是 Python 一套

---

## 依赖图

```text
TASK-085
   ↓
TASK-086
   ↓
TASK-087
   ↓
TASK-088 ──┐
           ├→ TASK-090 → TASK-091 → TASK-092 → TASK-093
TASK-089 ──┘
```
