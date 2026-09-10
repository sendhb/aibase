# A03 · 贯通指定任务参数，失败停止，拒绝无进展成功

状态：planned，尚未实施。仓库：双仓库。
依赖：A02（均需已独立验收）。
审计发现：F06、F12。契约：[CONTRACTS](../CONTRACTS.md) C1；使用 C2 接口，完整 eligibility 在 A04。
复审级别：架构复审：双端命令兼容。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

选中 TASK-A 就只能运行 TASK-A，不能执行时再选别的任务。
涉及 autoloop_coder/reviewer/launcher、相应 CLI shim、dispatcher/agent_adapter.py、dispatcher.py、state.py（仅未知结果转人工/禁重派）、downlink.py、telemetry/agent_downlink.py，以及 AIMonitor args 校验和相关测试。

## 实施要求

1. coder/reviewer 与 launcher 贯通 --task TASK-ID，要求配合 --once；各种 shim、本地 adapter 和远端 worker 都不能漏传。
2. wire args 恰好 [TASK-ID]；adapter/worker 内部映射 --task，不接受任意 flags。保留 task_start 的位置参数映射。
3. 预检按命令区分：coder 不能处理 done/in-review；reviewer 的 in-review 应当运行，不能沿用“in-review 一律 skipped”的旧全局预检。
4. start 非零、超时或异常时立即终止该候选，不覆盖 assignee，不调用 LLM。调用后再读取状态确认 start 确实生效。
5. 指定任务缺失/不可执行不得回退 pick_task。所有 subprocess 的 cwd 显式为注册且允许的项目根。
6. LLM 返回 0 后读取指定 TASK：coder 必须进入 in-review/done，并有该任务有效 verify；reviewer 必须形成相应审查处理结果。仍停原状态需返回非零与 no-progress，不记 allocation done。
7. 使用共享 verify 校验入口，后续 A05/A06 加强后自然生效；本卡不能自建另一套日期检查。
8. 队列命令 done 与任务 done 分开；skipped 只记录跳过，不记新产出。远端等待超时/回报未知不得自动当作可重试成功。
9. 无 --task 的本地旧循环保留入口；本包实际调用始终 --once，完整无进展预算在 B/C 处理。

## 必需验收

- [ ] A03-01：存在更早编号任务时，指定 TASK-003 的 fake provider 只收到 TASK-003。
- [ ] A03-02：本地和远端路径 argv 与 cwd 一致，空参数/额外 flag 被拒绝。
- [ ] A03-03：start 返回 1、抛异常、或返回 0 但状态未变，均不调用 provider。
- [ ] A03-04：指定不存在任务不会改其他任务；负责人字段不被错误覆盖。
- [ ] A03-05：coder 的 in-review 跳过与 reviewer 的 in-review 执行分别正确。
- [ ] A03-06：fake LLM 退出 0 但不推进任务，本轮与 dispatcher 均不报告完成。
- [ ] A03-07：实际 HTTP 下发单任务，worker 用 fake executable 写任务标记，回报能轮询到。
- [ ] A03-08：非法 args 不能注入 CLI flag 或 shell 命令。

建议测试名：test_phase_a_task_binding.py（两仓库按所辖接口补例）。

## 禁止与停止条件

不引入 Task/Run 数据库或租约，不宣称阻止跨机双重执行。依赖/负责人判定不足是 A04，不通过再复制一份简化 parser 绕过去。
