# A10 · 普通输出不再触发服务致命错误误杀

状态：planned，尚未实施。仓库：aibase。
依赖：A03（均需已独立验收）。
审计发现：F11。契约：[CONTRACTS](../CONTRACTS.md) C7。
复审级别：常规复审，provider 格式依据需核查。
先读 [EXECUTION](../EXECUTION.md)；交付使用 [模板](../HANDOFF-TEMPLATE.md)。

## 目标与范围

修复“Added test for HTTP 401 handling”被 kill 为 86。
涉及 llm.py、coder/reviewer 实际调用和输出扫描入口、相关 provider adapter 与测试；不改模型配置和 watchdog 生命周期。

## 实施要求

1. 枚举 scan_fatal_output 的所有调用者，不只改一个纯函数测试。
2. 正文、工具日志、代码和引用里的关键词不能直接导致 kill。
3. 支持的结构化 provider 控制事件要有本地格式依据与正负样例；没有可靠格式时使用进程退出/timeout，不杜撰错误协议。
4. stderr 也可能是正常业务日志，不能简单改成“stderr 里含 401 就 kill”。
5. 普通文本可以触发非阻断告警，必须与 fatal 返回码区分。
6. 保留非零退出、真正超时和已确认鉴权控制错误的可见性，不把所有异常转为成功。
7. 若要移动公共进程代码以复用 classifier，严格限定必要改动，不顺手实现 F07 进程树修复。

## 必需验收

- [ ] A10-01：正常进程讨论 HTTP 401/403、欠费、gateway_error 后退出 0，框架也正常结束。
- [ ] A10-02：相同字符串在 stdout/stderr、代码块、工具结果里均不误杀。
- [ ] A10-03：已证实的真实结构化错误 fixture 返回相应失败，而非成功。
- [ ] A10-04：无结构化支持的 provider 非零退出和 timeout 仍失败。
- [ ] A10-05：真实子进程输出正常讨论，实际调用链返回预期，不只 mock scan 函数。
- [ ] A10-06：原审计 fatal_false_positive 的 86 变为 0，其他错误码未混淆。

建议新增 kit/tests/test_phase_a_error_classification.py。

## 禁止与停止条件

不删掉全部错误处理，不扩大关键词列表求覆盖。真实 provider 格式无法核实则明确降级到退出码/timeout，并记录限制，不调用付费模型验证。
