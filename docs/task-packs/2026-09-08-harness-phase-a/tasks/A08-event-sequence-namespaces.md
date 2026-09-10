# A08 · 隔离服务端与 agent 事件序号，防止静默丢失

状态：planned，尚未实施。仓库：AIMonitor 为主。
依赖：A02、A07（均需已独立验收）。
审计发现：F04。契约：[CONTRACTS](../CONTRACTS.md) C5。
复审级别：架构复审：存储迁移与 cursor。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

同一项目中的 server 事件不能占据 agent 下一条 seq。涉及 IngestStore、ingest 调用者、服务端追加事件、events 查询及前端必要的排序/键处理。

## 实施要求

1. 按 C5 定义源身份和唯一键，server 接收排序与 agent seq 分开。
2. 认证层把真实 agent_id 传入 store；不能相信 payload 自报的 producer_id。
3. agent cursor 只随成功接收的 agent 流推进；server 插入不修改它。
4. 同 ID 同内容重发幂等；同 ID 不同内容冲突报错、事务回滚，不推进 cursor。
5. 在临时 DB 中测试旧表的 legacy 兼容/迁移。历史来源不明时保留为 legacy，不按字段猜成 agent。
6. 旧表不删除，迁移可重复执行且有版本标记/失败回滚；日志不得声称恢复已经丢失的历史事件。
7. 更新 events API/UI 需要的排序字段，旧 seq 不再被当作跨源全局唯一 key。保留项目权限接入位置供 A09 使用。

## 必需验收

- [ ] A08-01：agent seq=1→server event→agent seq=2，三条全在且内容正确。
- [ ] A08-02：相同 agent 事件重传一次只存一条，cursor 正确。
- [ ] A08-03：同 ID 异内容被拒绝，不静默 INSERT OR IGNORE。
- [ ] A08-04：server 事件不改变 agent 接收 cursor。
- [ ] A08-05：不同生产者同 seq 共存；HTTP 不能伪造另一个 agent 身份。
- [ ] A08-06：历史事件条数、内容与原 cursor 在兼容处理后保留。
- [ ] A08-07：迁移中故障可回滚，再执行不重复复制历史。
- [ ] A08-08：查询排序稳定，前端不因重复 seq 覆盖一条事件。

建议新增 test/test_phase_a_event_identity.py。

## 禁止与停止条件

不在真实 DB 上迁移；有未知 schema 时停用相关写入并报告，不重建空库。
producer epoch、多机接管、跨库 outbox 与事件日志恢复属于后续 B。
