# mcp/ — 能力层 MCP server（stdio）

> 把 tools/ 能力层包装为 **stdio MCP server**（TASK-097），兑现 ARCHITECTURE.md
> 「tools/ 能力层可包装为 MCP server」的承诺：宿主 CLI（pi / claude 等）对框架能力
> 从「模型读文档 → 手敲 shell」升级为**原生工具调用**。

## 接口

```bash
python3 kit/tools/mcp/server.py [--root DIR]
```

- **协议**：JSON-RPC 2.0 over stdio，换行分隔（MCP 稳定规范的 stdio 传输）
- **握手**：`initialize` → `notifications/initialized`（通知，不回应）→ `tools/list` / `tools/call`
- **stdout = 协议通道**；诊断信息一律 stderr。EOF / 管道断裂即退出。
- `--root`：项目根（默认从脚本位置向上探测 `aios.config.yaml`）

## 暴露的工具（最小集）

| 工具 | 能力 | 桥接方式 |
|------|------|---------|
| `task_list` | 列出任务（与 `task list` 同一表格文本，status 可选过滤） | import 复用 `kit/cli/task` 的 `cmd_list` |
| `task_start` | open/blocked → in-progress（含打回计数） | import 复用 `cmd_start` |
| `task_verify` | 真实执行 build/lint/test/check 并写 VERIFY 记录 | import 复用 `cmd_verify` |
| `task_done` | 关闭任务（治理闸门原样生效） | import 复用 `cmd_done` |
| `context_assemble` | 按 task 推导最小上下文装配清单（budget 缺省 600） | import 复用 `kit/cli/lib/context_loader.py` |

## 治理保持（红线）

- **只桥接不重实现**：task 工具走 CLI `cmd_*` 同一代码路径（SourceFileLoader
  import 复用，不经 subprocess），P0 approval-ref 校验、VERIFY/REVIEW 证据前置、
  状态机合法性与 CLI 直调**逐字一致**（单测断言：TASK-097 验收 4）。
- **不暴露 `--force`**：CLI 本身拒绝 `--force` 绕过关闭前置条件，MCP 通道无此参数。
- **不暴露网络端口**：仅 stdio，本地可信环境；安全边界 = 文件系统权限 + tasklib 治理校验。
- 工具执行失败（含治理拒绝）→ `result.isError: true` + CLI 原样错误文本（MCP 规范）；
  未知工具 / 缺必填参数 → JSON-RPC `-32602`。

## 实现约束

- **纯 stdlib 零第三方依赖**（验收 5：`kit/tools/mcp/*.py` 行首 import 全 stdlib）。
- first-party 桥接模块（task CLI / context_loader）一律**函数内动态加载**，
  保持行首 import 纯净。
- 单测：`kit/tests/test_mcp_server.py`（协议往返 / 任务集一致 / 状态迁移 /
  治理穿透 / stdlib 自检，stdin/stdout 替身跑协议）。
