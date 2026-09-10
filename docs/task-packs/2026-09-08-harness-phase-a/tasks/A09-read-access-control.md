# A09 · 保护监控读接口并接通 UI 与 dispatcher 认证

状态：planned，尚未实施。仓库：双仓库。
依赖：A07、A08（均需已独立验收）。
审计发现：F09。契约：[CONTRACTS](../CONTRACTS.md) C6；保留 C1 写权限边界。
复审级别：必须安全/架构复审。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

未授权访问不能读取项目状态、会话或衍生统计。
涉及 AIMonitor 配置加载、HTTP 读路由、启动参数、前端 fetch 和最小登录交互；AIOS dispatcher/probe.py 的读请求凭据。可补示例配置和部署说明，但只含占位假值。

## 实施要求

1. 实现 C6 的独立 readers 配置，缺失/损坏/权限不合格时 fail-closed；默认 loopback，显式 --host 才放宽监听。
2. 枚举所有项目内容读 API，不只给 sessions 加一处判断；静态页面可访问，私有数据需 Bearer。
3. 聚合 status/history/alerts/counts 只包含授权项目，不在总数、错误信息、查询参数或搜索中泄露其他项目。
4. 前端最小 token 输入/退出，仅内存保存；统一私有读 fetch。保留已有管理授权，不用 read token 请求写 API。
5. dispatcher 从 AIOS_MONITOR_READ_TOKEN 读取，只放 Authorization header；未配或 401/403 明确 skipped/配置错误，不返回完整空集。
6. 边界测试用两个项目、两个 reader、一个 writer/agent：读写身份不能互相升级。
7. token 比较、日志、请求异常、前端错误提示避免回显秘密。不会因为 --host=0.0.0.0 自动获得匿名读权。
8. 提供同源使用与 TLS/反向代理说明；真实网络改动、密码创建/分发由运维另行批准。

## 必需验收

- [ ] A09-01：未认证 status/history/events/sessions 均不能返回私有内容。
- [ ] A09-02：reader 只能读取允许项目，聚合统计没有越权项目贡献。
- [ ] A09-03：读 token 不可写 ingest/downlink/注册批准；agent token 不自动拥有读权限。
- [ ] A09-04：缺失、损坏、权限错误配置不回退匿名。
- [ ] A09-05：UI 输入后可读取，退出/刷新清除凭据；源码无 localStorage/URL token 持久化。
- [ ] A09-06：dispatcher 带正确 token 可读完整快照；不正确时停止该项目调度。
- [ ] A09-07：错误和日志不含 fake secret sentinel。
- [ ] A09-08：默认监听地址与显式 host 均测试；部署说明不把本地绑定等同于认证。
- [ ] A09-09：用临时页面服务完成浏览器手工或自动交互验收，记录方式；仅检查源码字符串不能代替 UI 验收。

建议新增 test/test_phase_a_read_auth.py、AIOS test_phase_a_monitor_auth.py。
没有浏览器测试能力时其他测试继续，但 A09 不可标 accepted，应交接 UI 验收 blocker。

## 禁止与停止条件

不复用生产 token、不关鉴权求兼容、不使用 * 默认全项目、不接受不受信任代理头作为身份。
认证方案要改变时先复审；不在本卡扩展成完整 SSO/RBAC 平台。
