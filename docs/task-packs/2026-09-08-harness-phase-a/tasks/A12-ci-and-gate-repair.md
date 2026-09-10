# A12 · 修复干净检出 CI、覆盖率口径和失败传播

状态：planned，尚未实施。仓库：双仓库。
依赖：A01、A02、A03、A04、A05、A06、A07、A08、A09、A10、A11（均需已独立验收）。
审计发现：F14。契约：[CONTRACTS](../CONTRACTS.md) 通用测试纪律；ACCEPTANCE。
复审级别：架构复审：发布门禁与供应链输入。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

让测试门禁真实覆盖主链路，不依赖开发者机器里恰好存在的 kit 和项目数据。
涉及两仓库 .github/workflows/verify.yml、测试入口、AIMonitor aios.config.yaml、registration_test.py；允许新增 kit/tools/validation/verify_phase_a.py。

## 实施要求

1. 各仓库 CI 执行自己的真实测试；AIMonitor 不能 checkout 后直接假定被忽略的 kit 存在。
2. 所需跨仓库源码显式 checkout 到指定路径并固定 SHA，不用 latest/main 或本机 ../ 的偶然状态。非秘密仓库地址/版本配置与私有仓库访问凭据分开。
3. 如 CI 尚缺仓库地址/访问授权，保留本地显式根路径运行能力，并报告阻塞；不能默默跳过跨仓库测试后绿灯。
4. 将 downlink_test 和各 Phase A 回归接入默认门禁，保留原测试覆盖与合理平台条件。
5. registration 覆盖范围用明确的类/函数名解析边界，不再硬编码行号；目标名称不存在必须失败。保持 85% 阈值，重新测量后不足就补有意义用例，不改阈值。
6. 添加相关服务模块同作用域重复类/方法检测。处理合法 @property setter 等重复命名要有精确例外，不能泛化成“所有重复都忽略”。
7. verify_phase_a.py 接收 --aios-root/--aimonitor-root，逐项记录命令、cwd、退出码、未执行项，任一必需项失败/缺失则总体非零。
8. 为 A13 提供必需 E2E 组入口；A13 尚未提交时显示 missing-e2e 并失败，不能预先填成功占位。A12 的 runner 单测通过不代表整个阶段通过。
9. 配置/任务包本身不授予部署、GitHub 设置变更或新增 secret 权限。Windows 和 Linux 分别说明执行与跳过原因，不声称未跑平台通过。

## 必需验收

- [ ] A12-01：无私有 runtime、无本地 kit 的干净临时检出，能按声明依赖运行或明确报告缺依赖。
- [ ] A12-02：下行首个 HTTP 请求故障会使最终门禁失败。
- [ ] A12-03：在 monitor_server.py 前插入无关空行/注释，覆盖对象与分母不漂移。
- [ ] A12-04：删一个覆盖目标名、移除一个必需测试、注入子命令非零，均不能绿色通过。
- [ ] A12-05：重复 DownlinkStore/handler 的 fixture 被静态检查捕获。
- [ ] A12-06：缺 sibling root、未执行 E2E、仅有观察 JSON 时，总体失败。
- [ ] A12-07：日志有实际两仓库版本组合、测试总数、跳过原因，没有秘密。
- [ ] A12-08：AIMonitor 默认 test 命令包含 downlink 回归；没有降低原覆盖阈值。

## 禁止与停止条件

不把未授权 CI 配置改动伪装为完成；无法获得依赖则 blocked，先提供本地复验。
不以复制整套可变 kit 到版本库的方式掩盖依赖声明；重大打包策略变更交架构复审。
