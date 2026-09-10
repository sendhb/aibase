# A01 · 隔离测试输入，建立可复验的测试基础

状态：planned，尚未实施。仓库：双仓库。
依赖：无；本包第一张执行卡。
审计发现：F14。契约：[CONTRACTS](../CONTRACTS.md) 通用执行协议；C9。
复审级别：常规独立复审。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

先修测试会读取真实 runtime、依赖真实 TASK-023 的问题，使后续 LLM 能安全验证。
只改测试、fixture/helper 与测试说明，不修业务状态机。
读取 aibase kit/tests/test_agent_smoke.py、AIMonitor test/server_smoke.py、registration_test.py 和原审计两个脚本的临时环境构造。

## 实施要求

1. 将 AIOS smoke 的项目目录改为 tempfile 合成项目；覆盖至少一个任务、一次游标推进、一个假会话。断言完整内容时不打印真实工作区。
2. AIMonitor smoke 不读取真实 projects.json/runtime；将 TASK-023 的业务假设改为明确的合成任务 fixture，保留原接口与计数断言的业务覆盖。
3. 引入轻量可复用 fixture：临时 Git 项目、fake provider、临时 HTTP server、fake token、临时数据库。源码位置可引用真实仓库，状态目录不能引用真实仓库。
4. 用 sentinel 项目证明测试没有读取/改变外部 runtime 和游标。敏感标记不出现在 stdout/stderr。
5. 所有自建进程/线程/端口在失败和成功路径均回收。不要仅依赖 TemporaryDirectory 删除正在使用的数据库。
6. 不修改审计历史 JSON；两个观察型脚本保持原意，后续回归另写真正断言。
7. 先运行局部测试，再在隔离副本执行旧套件。记录仍由 F02 等引起的失败，不改成 pass。

## 必需验收

- [ ] A01-01：真实仓库无任何 runtime 时，新的 smoke 仍可执行并通过相应断言。
- [ ] A01-02：合成项目游标确实推进，外部 sentinel 的文件内容/mtime 不变。
- [ ] A01-03：测试失败也不遗留 HTTP server 或自建子进程。
- [ ] A01-04：原 TASK-023 断言被合成数据覆盖，而不是直接删除测试。
- [ ] A01-05：输出没有真实会话、凭据、通知或外部服务连接。
- [ ] A01-06：给一个断言注入失败，测试命令实际非零。

建议新增 kit/tests/test_phase_a_isolation.py、test/test_phase_a_isolation.py；分别用 unittest discover 的 -p 指定文件运行。
本卡交付后先独立验收隔离性，再允许后续卡运行整套测试。

## 禁止与停止条件

不接触业务代码、生产数据库/配置，不为测试创建真实 TASK。不靠“先备份真实 runtime 再跑”作为长期方案。
测试必须访问外部服务才能通过时，提交依赖点和替代 fixture 方案，不申请真实业务凭据。
