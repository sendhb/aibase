# A05 · 使最新验证尝试成为唯一关闭依据

状态：planned，尚未实施。仓库：aibase。
依赖：A04（均需已独立验收）。
审计发现：F01。契约：[CONTRACTS](../CONTRACTS.md) C3 的 attempt 部分；指纹由 A06 完成。
复审级别：架构复审：失败、崩溃与历史兼容。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

消除“先通过、后失败、仍能用旧 pass 关闭”。涉及 task CLI、tasklib 证据入口；允许新增 kit/cli/lib/verification_state.py。
这是 A06 的前置部分；本卡单独完成不代表证据已绑定代码，不部署中间版本。

## 实施要求

1. verify 在运行命令前建立唯一 attempt，并原子设为 current/running。
2. 每个步骤真实记录退出码；任一步失败、异常、启动失败、写状态失败均阻止最终 pass。
3. done/approve 只接受 current 最终 pass；旧 Markdown pass、日期相同、任意另一个成功 attempt 都不能覆盖 current fail/running。
4. Markdown VERIFY 仍可导出供人读，但新增状态必须单源；不得将两种证据以“任意一种满足”连接。
5. 写入采用原子替换与必要锁；保持失败原始证据。模拟进程在开始记录后崩溃，running 必须保持不可关闭。
6. 兼容历史 done 展示；不自动重开历史任务。旧未绑定 VERIFY 用于新关闭时明确提示重新验证。
7. 固定 C3 字段，A06 未提供的指纹标注未绑定；本卡过渡测试可只验证 attempt，A06 接入前整个 Phase A 不可验收。

## 必需验收

- [ ] A05-01：先 pass→再 fail→done 和 approve 都失败。
- [ ] A05-02：先 pass→新 running→崩溃，旧 pass 不可用。
- [ ] A05-03：fail→重新真实 pass，current 切换到新 ID，不删除历史。
- [ ] A05-04：状态 JSON 损坏、未知 schema、写盘失败都拒绝关闭。
- [ ] A05-05：单独写一个旧格式 VERIFY 不可关闭新任务。
- [ ] A05-06：历史已 done 的任务只读查看正常，不被批量重开。
- [ ] A05-07：多次同日验证有不同 ID，选择不依赖 os.listdir/mtime 顺序。

建议新增 kit/tests/test_phase_a_verification_attempts.py。修复前失败用例源于审计 stale_evidence。

## 禁止与停止条件

不以删除所有 VERIFY 为修复，不清空真实 runtime，不用 try/except 忽略写盘异常。
状态、导出与 CLI 锁顺序有冲突时先画出调用路径交付复审，不能去掉锁求通过。
