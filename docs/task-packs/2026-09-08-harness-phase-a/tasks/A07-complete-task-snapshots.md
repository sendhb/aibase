# A07 · 遥测传递完整任务元数据，不完整时禁止远端调度

状态：planned，尚未实施。仓库：双仓库。
依赖：A02、A04（均需已独立验收）。
审计发现：F03。契约：[CONTRACTS](../CONTRACTS.md) C4；复用 C2 字段语义。
复审级别：必须架构复审：容量、兼容与未知状态。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

解决前 50 条 done 挤掉后续 open、旧角色日志冒充最近进展，以及远端补造审批/负责人字段。
涉及 telemetry/agent_runtime.py、agent_payload.py、agent_loop.py、dispatcher/probe.py/policy.py；AIMonitor ingest 校验、存储、status 序列化和必要 UI 标识。

## 实施要求

1. 按 C4 新增 task_snapshot，保留旧展示结构；全部任务元数据在容量内可完整传输。
2. 先保候选事实与总数，再按预算减少展示/会话字段；完整性未知时不能只传空数组。
3. 61 个任务场景中 11 个 open 必须进入结构化元数据；展示优先活动任务。
4. 超容量、解析错误、任务目录读取错误要使 complete=false，给出原因，心跳仍可发送。
5. server 验证类型、总数和完整性，原样保留重要元数据；status 输出服务端接收时间和完整性。
6. probe 不拼造风险/审批/返工/负责人，使用结构化数据适配 C2；缺失字段、旧 schema、离线、过期分别 skipped，不声称没有工作。
7. 角色日志截取最后完整行，保留最新事件；不得吞掉半截 JSON 后伪造一条完整事件。
8. old client 可继续监控显示；自动调度要求双方能力就绪。文档明确先升级服务端、再 agent/dispatcher 的兼容矩阵，但本任务不部署。

## 必需验收

- [ ] A07-01：前 50 done + 后 11 open，服务端与 dispatcher 都看得到 11 个 open。
- [ ] A07-02：61 条元数据的依赖、负责人、风险、审批引用与源一致。
- [ ] A07-03：超 256 KiB 情况 complete=false，远端不派任何任务。
- [ ] A07-04：目录错误、单任务解析错误、计数矛盾都不能伪装 complete=true。
- [ ] A07-05：旧 payload、旧 server 响应和过期快照明确 skipped，不回退不安全选择。
- [ ] A07-06：完整空任务集是正常无任务，与未知/不完整可区分。
- [ ] A07-07：长角色日志末尾标记经过真实 ingest/status 保留。
- [ ] A07-08：发送端不修改入参，不推进未发送事件/会话的 cursor。
- [ ] A07-09：真实 HTTP 往返保留新增字段，而不是只测 build_payload 字典。

建议测试名 test_phase_a_snapshot.py（两端）；用可注入时钟，不 sleep 等待过期。

## 禁止与停止条件

不通过增大 payload 上限、删除所有 done 或把总数改成发送数消除红灯。
本卡不构建完整远端任务数据库/分页服务；超过受限容量就明确停调度，后续 B 再扩展。
