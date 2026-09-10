# A02 · 收敛 AIMonitor 下行实现并修复真实 HTTP

状态：planned，尚未实施。仓库：AIMonitor 为主。
依赖：A01（均需已独立验收）。
审计发现：F02。契约：[CONTRACTS](../CONTRACTS.md) C1（单任务 args 收紧由 A03 完成）。
复审级别：架构复审：HTTP / schema / 旧数据。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

修复入队、领取、轮询、回报的三类断连与重复定义。主文件 server/monitor_server.py；可抽取 server/downlink_store.py。测试范围 test/downlink_test.py 与新增 test/test_phase_a_downlink.py。

## 实施要求

1. 列出重复类、方法、正则和 State 重复初始化，确定实际生效路径；删除冗余实现而不是继续追加第三套。
2. 按 C1 使用字符串 command_id；所有 HTTP 路由、存储、序列化、测试辅助都按不透明 ID 处理。
3. 保留现有外层请求/响应形状，统一 enqueue/pickup/get/result 的内部参数；只保留一个实例。
4. store 的领取必须在 SQLite 写事务中条件更新，记录 picked_by；返回结果不能依靠客户端自报任意 agent 身份。
5. 入队/读状态/回报均检查项目授权；同 key 不同内容不能被客户端当作正常复用。此处若需要改 AIOS adapter 的 conflict 响应识别，仅限该小变更并记录双仓库差异。
6. HTTP 异常有明确状态码和 JSON；不得以 catch Exception 后返回空成功解决断连。
7. 旧整数 ID 用例可按契约升级断言与 URL 格式；保留原来的授权、FIFO、超时、重放覆盖，不清空用例。
8. 按 C1 明确拒绝未知/有数据旧队列，提供本地升级说明。只测试临时 DB，不操作现有队列。
9. 字符串数组 args 暂保留兼容校验；A03 后禁止空任务参数。终态语义、幂等回报和权限本卡即落实。

## 必需验收

- [ ] A02-01：真实 HTTP 入队→领取→回报→轮询终态完整成功，不 mock handler/store 主路径。
- [ ] A02-02：空队列为 200/command=null，未知 ID 为 404。
- [ ] A02-03：非法 JSON、数组代替对象、bool timeout/exit_code 返回明确 4xx，不断连。
- [ ] A02-04：无 token、越项目、非领取者报告分别被拒绝。
- [ ] A02-05：重复入队复用正确；异内容冲突不改变原命令；重复相同回报成功，冲突回报不覆盖。
- [ ] A02-06：两个并发 pickup 请求不能领取同一条命令。
- [ ] A02-07：重启临时 store 后 ID 不回退；旧 DB 受保护，不静默清空。
- [ ] A02-08：同作用域重复 DownlinkStore/handler 的 AST 检查通过。

局部命令：python3 -B -m unittest discover -s test -p test_phase_a_downlink.py。A01 隔离后再运行原 downlink_test.py。

## 禁止与停止条件

不重构整个监控服务、不上消息队列、不加入分布式租约。旧数据库需要迁移时只报告 schema 与计划，不进行真实迁移。
完成不等于跨机执行已恢复：必须等 A03、A07、A09 和 A13 联调。
