# A06 · 将验证绑定到当前工件、测试和任务验收口径

状态：planned，尚未实施。仓库：aibase。
依赖：A05（均需已独立验收）。
审计发现：F01、F08。契约：[CONTRACTS](../CONTRACTS.md) C3 全部；C9；ROLE-INDEPENDENCE。
复审级别：必须架构复审：输入范围与失效策略。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

在 A05 的 attempt 上加入确定性指纹，验证后改坏代码/数据/验收配置不能关闭。
涉及 verification_state.py、tasklib、task verify/done/approve、必要的 config 校验与测试。不接入独立身份/执行容器。

## 实施要求

1. 严格实现 C3 的输入枚举、排除项、前后 hash、current 校验；输出只记录摘要和路径，不打印文件内容。
2. 不只保护 .py/.ts：文档、JSON/CSV、资源元数据同样是工件；不检测引擎存在。
3. 未提交改动、staged 改动、删除、重命名与新增非忽略文件都纳入；验证时改输入即失败。
4. TASK 说明/验收标准另绑定；仅 status/updated 的受控变化不使证据自失效。
5. config.commands、生成目录设置、测试文件和依赖锁变化都使证据失效；不能用修改 source_dirs 来隐藏改动。
6. 拒绝 C3 规定的未知输入类型/根外链接/不支持子模块；给出可行动错误。无 Git 只诊断、不自动关闭，不擅自初始化 Git。
7. 完成入口与 A03 的后置检查统一调用同一个校验；REVIEW 需要的旧判定仍存在，但不得越过 verify 指纹。
8. 将受限 Git 工件模式和大资产/外部依赖局限写入工具说明，不宣称恶意 agent 已不能伪造结果。

## 必需验收

- [ ] A06-01：同一 HEAD 下改一个源码字节，done 拒绝。
- [ ] A06-02：改测试、commands、依赖锁、生成目录配置，均拒绝复用。
- [ ] A06-03：新增/删除/重命名非忽略输入、改变 index 状态，均被识别。
- [ ] A06-04：改 TASK 验收正文或 reviewer/risk 使证据失效；仅合法 status/updated 变化不失效。
- [ ] A06-05：验证命令边运行边改输入，最终不能产生可用 pass。
- [ ] A06-06：心跳、日志、验证导出、已知指标写入不触发自身无限失效。
- [ ] A06-07：不可读文件、根外 symlink、子模块/无 Git 等不支持输入明确拒绝，不漏算成功。
- [ ] A06-08：Git 中策划数据/资源元数据修改与代码修改同等失效。
- [ ] A06-09：回退工件到旧 pass 的内容，但 current 是更新的失败，仍不能使用旧记录。

建议新增 kit/tests/test_phase_a_verification_fingerprint.py；所有 Git 仓库是临时 fixture。

## 禁止与停止条件

不得放宽到“只比 HEAD”，不得默认忽略 tests/docs/assets 或全部 untracked。
如果指纹计算会读取真实密钥/外部仓库或无法明确枚举输入，停止报告，不能通过省略输入来通过测试。
完整工件/环境固定与可信验证权限留待 C，不把本卡当作安全认证。
