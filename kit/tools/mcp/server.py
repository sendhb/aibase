#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ====================================================================
# 本脚本是 Python 3，请勿用 bash/sh 直接调用（防护同 kit/cli/task 头注释：
# bash 会把 Python 源码当 shell 逐行解析，产生巨量错误输出甚至死循环）。
# 正确调用：python3 kit/tools/mcp/server.py  或  ./kit/tools/mcp/server.py
# 下面两行是【bash/Python 双语法】防护（勿改动、勿合并）：
#   - Python：相邻字符串字面量隐式拼接，作为无副作用表达式（no-op）；
#   - bash/sh/zsh：`echo` 打印错误提示，`exit 1` 立即退出，
#     不会继续解析后面的 Python 源码 —— 从根上杜绝 bash 误调用死循环。
# ====================================================================
"echo" "✗ 本脚本是 Python 3，请勿用 bash/sh 调用。正确用法: python3 kit/tools/mcp/server.py"
"exit" "1"
"""server.py — AIOS 能力层 MCP server（TASK-097）

把 tools/ 能力层包装为 stdio MCP server，兑现 ARCHITECTURE.md「tools/ 能力层
可包装为 MCP server」的既有承诺：宿主 CLI（pi / claude 等）对 task 状态机与
上下文装配从「模型读文档 → 手敲 shell」升级为**原生工具调用**。

协议：JSON-RPC 2.0 over stdio，换行分隔（MCP 稳定规范的 stdio 传输）：
  initialize → notifications/initialized（通知，不回应）→ tools/list → tools/call

暴露的工具（最小集，与 CLI 行为逐字一致 —— import 复用同一代码路径）：
  task_list / task_start / task_verify / task_done —— 桥接 kit/cli/task
      （SourceFileLoader 加载 cmd_* 复用，不经 subprocess；治理闸门与
        错误信息与 CLI 直调逐字一致）
  context_assemble —— 桥接 kit/cli/lib/context_loader.py（TASK-094 装配器）

治理保持（TASK-097 范围红线）：
  - 只桥接不重实现：P0 approval-ref 校验 / VERIFY 证据前置 / 状态机合法性
    原样穿透 —— MCP 通道不开放任何绕过治理的路径；
  - 不暴露 --force（CLI 本身也拒绝 --force 绕过关闭前置条件）；
  - 仅 stdio 本地传输，不暴露网络端口；stdout 是协议通道，诊断一律走 stderr。

纯 stdlib 零第三方依赖（验收 5：行首 import 全 stdlib；first-party 桥接模块
一律函数内动态加载，见 load_task_cli / Bridge.context_loader）。

用法：
  python3 kit/tools/mcp/server.py [--root DIR]
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys

SERVER_NAME = "aios-kit-tools"
SERVER_VERSION = "0.1.0"
LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

# JSON-RPC 2.0 标准错误码（协议层；工具执行失败走 result.isError=true，MCP 规范）
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# 工具 schema 的状态枚举（与 tasklib.STATUSES 一致；test_mcp_server 有漂移护栏）
STATUSES = ["open", "in-progress", "in-review", "blocked", "done", "cancelled"]


class RpcError(Exception):
    """协议层错误（映射为 JSON-RPC error response）。工具执行失败不用它。"""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------- 项目根 / first-party 模块加载 ----------------

def find_root(start):
    """从 start 向上找含 aios.config.yaml 的目录（tasklib.find_project_root 等价实现）。

    独立小实现而非 import tasklib：保持行首 import 全 stdlib（验收 5），
    协议循环启动前不引入 first-party 状态。找不到返回 None。
    """
    d = os.path.dirname(os.path.abspath(start))
    while True:
        if os.path.isfile(os.path.join(d, "aios.config.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def cli_paths(root):
    """(task CLI 路径, lib 目录)：kit 布局 <root>/kit/cli/ 与平铺 <root>/cli/ 兼容
    （与 tasklib.find_project_root 的布局约定一致）。"""
    kit_cli = os.path.join(root, "kit", "cli")
    flat_cli = os.path.join(root, "cli")
    base = kit_cli if os.path.isfile(os.path.join(kit_cli, "task")) else flat_cli
    return os.path.join(base, "task"), os.path.join(base, "lib")


def load_task_cli(code_root, runtime_root=None):
    """import 复用 kit/cli/task（无 .py 后缀脚本 → SourceFileLoader，与既有单测同手法），
    并把运行时目录绑定到 runtime_root。

    - code_root：CLI 代码所在项目根（定位 kit/cli/task 与 lib/）
    - runtime_root：runtime/ 数据根；生产=项目根本身，测试=临时 fixture
    - 每次调用返回独立模块实例：测试绑定临时目录互不污染；
      cmd_* 与 CLI 直调走完全相同的代码路径（治理闸门原样穿透）
    """
    task_path, _ = cli_paths(code_root)
    if not os.path.isfile(task_path):
        raise FileNotFoundError(f"未找到 task CLI: {task_path}")
    runtime_root = runtime_root or code_root
    name = "aios_task_cli_mcp"
    loader = importlib.machinery.SourceFileLoader(name, task_path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    runtime = os.path.join(runtime_root, "runtime")
    mod.AI_DIR = runtime_root
    mod.RUNTIME_DIR = runtime
    mod.TASKS_DIR = os.path.join(runtime, "tasks")
    mod.STATE_DIR = os.path.join(runtime, "states")
    mod.VERIFY_DIR = os.path.join(runtime, "verification")
    mod.REVIEW_DIR = os.path.join(runtime, "reviews")
    mod.LOG_DIR = os.path.join(runtime, "logs")
    mod.EVENTS_LOCK = os.path.join(runtime, "locks", "task-events.lock")
    # touch_focus / append_blocker 直写 STATE_DIR 不自行建目录；事件锁需要 locks/ 可创建
    os.makedirs(mod.STATE_DIR, exist_ok=True)
    os.makedirs(mod.LOG_DIR, exist_ok=True)
    return mod


def run_cli(fn, *args):
    """调 CLI 命令函数：捕获 stdout/stderr 与 SystemExit（tasklib.die 的控制流）。

    返回 (exit_code, stdout, stderr)。失败路径的 stderr 文本与 CLI 直调逐字一致
    —— 这是「治理闸门穿透」的载体（验收 4）。
    """
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            fn(*args)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return code, out.getvalue(), err.getvalue()


class Bridge:
    """工具桥接层：持有数据根（root）与代码根（code_root），first-party 模块懒加载。"""

    def __init__(self, root, code_root=None):
        self.root = root
        self.code_root = code_root or root
        self._task = None

    @property
    def task(self):
        if self._task is None:
            self._task = load_task_cli(self.code_root, self.root)
        return self._task

    def context_loader(self):
        _, lib_dir = cli_paths(self.code_root)
        if lib_dir not in sys.path:
            sys.path.insert(0, lib_dir)
        import context_loader  # noqa: E402 —— first-party 函数内加载，行首 import 保持纯 stdlib（验收 5）
        return context_loader


# ---------------- 工具定义与桥接 ----------------

_TASK_ID_SCHEMA = {"type": "string", "minLength": 1,
                   "description": "任务 ID（短 id，如 TASK-097）"}

_TOOL_DEFS = []      # tools/list 载荷（注册序稳定）
_TOOL_HANDLERS = {}  # name -> fn(bridge, arguments) -> (text, is_error)


def tool(name, description, input_schema):
    """注册一个 MCP 工具：定义进 tools/list，实现进 tools/call 分发表。"""
    def deco(fn):
        _TOOL_DEFS.append({"name": name, "description": description,
                           "inputSchema": input_schema})
        _TOOL_HANDLERS[name] = fn
        return fn
    return deco


def _run_task_cmd(bridge, cmd_name, args):
    """task_* 工具公共桥：调 CLI 模块的 cmd_* 同名函数，失败即 CLI 原样报错文本。"""
    code, out, err = run_cli(getattr(bridge.task, "cmd_" + cmd_name), args)
    if code != 0:
        return (err or out).strip(), True
    return out.rstrip("\n"), False


@tool("task_list",
      "列出任务（与 `task list` 输出一致的表格文本）。status 可选过滤。",
      {"type": "object",
       "properties": {"status": {"type": "string", "enum": STATUSES,
                                 "description": "按状态过滤，缺省全量"}}})
def _tool_task_list(bridge, arguments):
    status = arguments.get("status")
    return _run_task_cmd(bridge, "list", [status] if status else [])


@tool("task_start",
      "开始任务：open/blocked → in-progress（与 `task start` 同一路径，含打回计数）。",
      {"type": "object", "required": ["task_id"],
       "properties": {"task_id": _TASK_ID_SCHEMA}})
def _tool_task_start(bridge, arguments):
    return _run_task_cmd(bridge, "start", [arguments["task_id"]])


@tool("task_verify",
      "真实执行 build/lint/test/check 并写 VERIFY 记录（与 `task verify` 同一代码路径，非自证）。",
      {"type": "object", "required": ["task_id"],
       "properties": {"task_id": _TASK_ID_SCHEMA}})
def _tool_task_verify(bridge, arguments):
    return _run_task_cmd(bridge, "verify", [arguments["task_id"]])


@tool("task_done",
      "关闭任务（与 `task done` 同一路径：VERIFY/REVIEW 证据与 P0 approval-ref 闸门原样生效；"
      "不暴露 --force，CLI 本身也拒绝绕过关闭前置条件）。",
      {"type": "object", "required": ["task_id"],
       "properties": {"task_id": _TASK_ID_SCHEMA}})
def _tool_task_done(bridge, arguments):
    return _run_task_cmd(bridge, "done", [arguments["task_id"]])


@tool("context_assemble",
      "按 task 推导最小上下文装配清单（路径/行数/预算占比三列；TASK-094 装配器）。"
      "budget 缺省 600 行。",
      {"type": "object", "required": ["task_id"],
       "properties": {"task_id": _TASK_ID_SCHEMA,
                      "budget": {"type": "integer", "minimum": 1,
                                 "description": "总行预算（缺省 600，AGENTS.md 原则推导）"}}})
def _tool_context_assemble(bridge, arguments):
    task_id = arguments["task_id"]
    budget = arguments.get("budget")
    try:
        plan = bridge.context_loader().assemble(task_id, root=bridge.root, budget=budget)
    except (TypeError, ValueError) as e:
        return f"✗ budget 参数非法: {e}", True
    if plan is None:
        return f"✗ 未找到任务: {task_id}（runtime/tasks/ 下不存在）", True
    return plan.render(), False


def _required_of(name):
    for d in _TOOL_DEFS:
        if d["name"] == name:
            return d["inputSchema"].get("required", [])
    return []


def call_tool(bridge, name, arguments):
    """执行一个工具调用（tools/call 的实现核心）。

    返回 MCP tool result（content + isError）。工具执行失败（含治理拒绝）→
    isError=true 的 result（MCP 规范），而非协议层 error；
    未知工具 / 缺必填参数 → 协议层 INVALID_PARAMS。
    """
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise RpcError(INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = arguments if isinstance(arguments, dict) else {}
    missing = [k for k in _required_of(name) if not str(arguments.get(k) or "").strip()]
    if missing:
        raise RpcError(INVALID_PARAMS, f"缺少必填参数: {', '.join(missing)}")
    try:
        text, is_error = handler(bridge, arguments)
    except FileNotFoundError as e:  # 桥接目标缺失（如 fixture 无 CLI）
        text, is_error = f"✗ {e}", True
    except SystemExit as e:  # 防御：die() 逃逸不得杀死协议循环
        text, is_error = f"✗ 工具异常退出（exit={e.code}）", True
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ---------------- JSON-RPC 2.0 分发 ----------------

def _error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _on_initialize(server, params):
    requested = params.get("protocolVersion")
    version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
    return {"protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}


def _on_ping(server, params):
    return {}


def _on_tools_list(server, params):
    return {"tools": [dict(d) for d in _TOOL_DEFS]}


def _on_tools_call(server, params):
    return call_tool(server.bridge, params.get("name"), params.get("arguments"))


_METHODS = {
    "initialize": _on_initialize,
    "ping": _on_ping,
    "notifications/initialized": lambda server, params: {},
    "tools/list": _on_tools_list,
    "tools/call": _on_tools_call,
}


def handle_message(line, server):
    """处理一行 JSON-RPC 消息 → 响应 dict；通知（无 id）返回 None（不回应）。"""
    try:
        msg = json.loads(line)
    except ValueError:
        return _error(None, PARSE_ERROR, "Parse error")
    if (not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0"
            or not isinstance(msg.get("method"), str)):
        msg_id = msg.get("id") if isinstance(msg, dict) else None
        return _error(msg_id, INVALID_REQUEST, "Invalid Request")
    handler = _METHODS.get(msg["method"])
    if handler is None:
        if "id" not in msg:
            return None  # 未知通知按 JSON-RPC 规范静默忽略
        return _error(msg["id"], METHOD_NOT_FOUND, f"Method not found: {msg['method']}")
    try:
        result = handler(server, msg.get("params") or {})
    except RpcError as e:
        if "id" not in msg:
            return None
        return _error(msg["id"], e.code, e.message)
    except Exception as e:  # noqa: BLE001 —— 单条消息异常不得中断协议循环
        if "id" not in msg:
            return None
        return _error(msg["id"], INTERNAL_ERROR, f"Internal error: {type(e).__name__}: {e}")
    if "id" not in msg:
        return None
    return {"jsonrpc": "2.0", "id": msg["id"], "result": result}


class Server:
    """一次会话的协议状态（当前无会话态；bridge 持有 first-party 模块缓存）。"""

    def __init__(self, root, code_root=None):
        self.bridge = Bridge(root, code_root)


def serve(stdin=None, stdout=None, root=None, code_root=None):
    """协议主循环：逐行读 JSON-RPC 请求 → 回写响应。EOF / 管道断裂即退出。

    stdin/stdout 可注入替身（io.StringIO）—— 单测以此跑协议往返（验收 1）。
    """
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    server = Server(root, code_root)
    try:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            resp = handle_message(line, server)
            if resp is not None:
                stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
                stdout.flush()
    except (BrokenPipeError, KeyboardInterrupt):
        return  # 对端关闭 / 中断：正常退出


def main(argv=None):
    # stdio 固定 UTF-8：JSON 含中文（任务描述/错误信息），不受 locale 影响
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        prog="mcp-server",
        description="AIOS 能力层 MCP server（stdio JSON-RPC 2.0，TASK-097）")
    parser.add_argument("--root", default=None,
                        help="项目根（默认从脚本位置向上探测 aios.config.yaml）")
    args = parser.parse_args(argv)
    root = args.root or find_root(__file__)
    if root is None:
        print("✗ 未找到项目根（向上找不到 aios.config.yaml）；用 --root 指定", file=sys.stderr)
        return 1
    serve(root=root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
