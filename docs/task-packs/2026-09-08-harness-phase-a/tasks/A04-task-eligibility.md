# A04 · 统一依赖、负责人、风险与指定审查者检查

状态：planned，尚未实施。仓库：aibase。
依赖：A03（均需已独立验收）。
审计发现：F06、F08。契约：[CONTRACTS](../CONTRACTS.md) C2；C9；ROLE-INDEPENDENCE。
复审级别：架构复审：人工入口与自动领取边界。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

让本地/远端候选选择与执行前检查使用一致的 eligibility。
涉及 tasklib.py、coder/reviewer、dispatcher/policy.py/governance.py 与相关测试；只为必要的公共判定调整 task CLI，不重写全部 YAML。

## 实施要求

1. 按 C2 返回结构化的 allowed/reason，不合格理由能在日志/候选结果中看到。
2. 完整扫描候选，再排序；首任务被阻塞时继续检查后续任务。
3. 检查依赖存在与 done、循环、未知格式；跨项目依赖明确不支持，禁止当作完成。
4. 不覆盖具体人类/其他 worker 的 assignee；指定 reviewer 必须匹配，且不等于实现者。
5. risk/priority 取更高等级，显式未知值失败；不把缺审批 P0 转给另一个入口自动执行。
6. 自动执行前再次检查，现有单项目锁内完成受控状态/负责人变更。抽取 unlocked helper 避免嵌套锁死；证明 CLI 与库调用路径都可用。
7. 不擅自把拒绝任务写成 blocked 或修改其依赖图；人工恢复入口如果需要覆盖负责人，应保持显式授权，不自动猜测。
8. backend/策划/资源/QA 项目使用相同通用检查，不添加“必须检测到 Unity/Unreal”的选题条件。

## 必需验收

- [ ] A04-01：TASK-001 依赖 blocked 项，TASK-003 就绪；选择 TASK-003。
- [ ] A04-02：missing/cancelled/循环依赖不可执行；done 依赖可执行。
- [ ] A04-03：human-alice 的任务不被 autoloop 抢走；any 和自身负责人可接。
- [ ] A04-04：指定 bob 的审查任务 eve 不可接，bob 可接，作者自己不可审。
- [ ] A04-05：priority=P0/risk=P2 在本地与中央行为相同；未知风险拒绝。
- [ ] A04-06：首项无审批、后项可执行时，后项得到机会。
- [ ] A04-07：同组参数化向量同时测试本地与中央；没有只对其中一边生效。
- [ ] A04-08：人工修复状态的合法路径未被自动领取条件意外锁死。
- [ ] A04-09：不含引擎目录的合成项目同样可被正常选中。

建议新增 kit/tests/test_phase_a_eligibility.py。

## 禁止与停止条件

不把字符串 reviewer/approval-ref 说成已实现可信身份；不实现跨项目 DAG 或岗位智能路由。
若为了共用判定必须改变远端 schema，按 A07 的 C4 做接口适配，不自造缺省事实。
