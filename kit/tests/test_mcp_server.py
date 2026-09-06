"""TASK-097 — 能力层 MCP server 单测。

覆盖（对照验收标准）：
- 协议往返：initialize + tools/list（stdin/stdout 替身）→ 合法 JSON-RPC 响应，5 工具在列
- task_list：tools/call 结果与 CLI `task list` 同一 runtime fixture 下任务集一致
- task_start：tools/call 后 tasklib 读出状态 in-progress
- task_done：happy path 关闭成功；P0 无 approval-ref 被拒，错误信息与 CLI 直调逐字一致
- context_assemble：桥接 TASK-094 装配器，render/预算/缺任务错误
- stdlib 自检：kit/tools/mcp/*.py 行首 import 全部 stdlib（零第三方依赖）
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # kit/tests → kit → 仓库根
_MCP_PATH = os.path.join(_ROOT, "kit", "tools", "mcp", "server.py")

# server.py 无 .py 包语境，用 SourceFileLoader 加载（与 test_task_events 同手法）
_loader = importlib.machinery.SourceFileLoader("mcp_server_test", _MCP_PATH)
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
mcp = importlib.util.module_from_spec(_spec)
_loader.exec_module(mcp)

_FALLBACK_STDLIB = {"argparse", "contextlib", "importlib", "io", "json", "os",
                    "sys", "re", "datetime", "subprocess", "tempfile",
                    "unittest", "pathlib", "time", "threading"}


def _task_md(name, status="open", risk="P2", approval="none", boxes=True):
    box = "- [x] 占位验收项" if boxes else "- [ ] 占位验收项"
    return (
        f"---\nname: {name}\ndescription: 测试任务 {name}\nmetadata:\n"
        f"  type: task\n  status: {status}\n  created: 2026-09-01\n"
        f"  updated: 2026-09-01\n  priority: P2\n  risk: {risk}\n"
        f"  approval-ref: {approval}\n  assignee: any\n  reviewer: any\n"
        f"  parent: TASK-000\n  depends-on: []\n  rework-count: 0\n"
        f"  tags: [test]\n---\n\n# {name}\n\n## 验收标准\n\n{box}\n"
    )


class Fixture:
    """临时 runtime fixture：aios.config.yaml + 任务集 + 可选 VERIFY 记录。"""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "runtime" / "tasks").mkdir(parents=True)
        (self.root / "aios.config.yaml").write_text(
            "version: 1\nprofile: test\nsource_dirs:\n  - runtime/\n"
            "generated_dirs: []\ncommands:\n  build: \"true\"\n  lint: \"true\"\n"
            "  test: \"true\"\n  check: \"true\"\n", encoding="utf-8")

    def add_task(self, name, **kw):
        p = self.root / "runtime" / "tasks" / (name + ".md")
        p.write_text(_task_md(name, **kw), encoding="utf-8")
        return p

    def add_verify(self, task_id):
        d = self.root / "runtime" / "verification"
        d.mkdir(parents=True, exist_ok=True)
        today = date.today().isoformat()
        stem = f"VERIFY-{today}-{task_id.lower()}"
        (d / (stem + ".md")).write_text(
            f"---\nname: {stem}\ndescription: 测试夹具验证记录\nmetadata:\n"
            f"  type: verify\n  date: {today}\n  task-ref: {task_id}\n"
            f"  verifier: test-fixture\n  result: pass\n  commit: n/a\n---\n\n# VERIFY\n",
            encoding="utf-8")

    def cleanup(self):
        self.tmp.cleanup()


def _rpc(fx, method, params=None, msg_id=None):
    """一次 JSON-RPC 往返（stdin/stdout 替身）。msg_id=None 发通知（不回应）。"""
    msg = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    out = io.StringIO()
    mcp.serve(stdin=io.StringIO(json.dumps(msg) + "\n"), stdout=out,
              root=str(fx.root), code_root=_ROOT)
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    return lines[0] if lines else None


def _call(fx, name, arguments, msg_id=10):
    return _rpc(fx, "tools/call", {"name": name, "arguments": arguments}, msg_id)["result"]


class ProtocolRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.cleanup()

    def test_initialize_and_tools_list_roundtrip(self):
        self.assertIsNone(_rpc(self.fx, "notifications/initialized"))  # 通知不回应
        init = _rpc(self.fx, "initialize",
                    {"protocolVersion": "2025-06-18", "clientInfo": {"name": "t"}}, 1)
        self.assertEqual(init["jsonrpc"], "2.0")
        self.assertEqual(init["id"], 1)
        self.assertEqual(init["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(init["result"]["serverInfo"]["name"], mcp.SERVER_NAME)
        self.assertIn("tools", init["result"]["capabilities"])
        tools = _rpc(self.fx, "tools/list", {}, 2)
        names = [t["name"] for t in tools["result"]["tools"]]
        self.assertEqual(names, ["task_list", "task_start", "task_verify",
                                 "task_done", "context_assemble"])
        for t in tools["result"]["tools"]:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])

    def test_initialize_echoes_supported_version(self):
        init = _rpc(self.fx, "initialize", {"protocolVersion": "2024-11-05"}, 1)
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

    def test_status_enum_matches_tasklib(self):
        # 漂移护栏：tools/list 的 status 枚举与 task CLI 实际 STATUSES 一致
        mod = mcp.load_task_cli(_ROOT, str(self.fx.root))
        self.assertEqual(mcp.STATUSES, list(mod.STATUSES))

    def test_protocol_errors(self):
        # 非法 JSON → -32700（id=null）；未知方法 → -32601；未知工具/缺必填参数 → -32602
        out = io.StringIO()
        mcp.serve(stdin=io.StringIO("{bad json\n"), stdout=out,
                  root=str(self.fx.root), code_root=_ROOT)
        resp = json.loads(out.getvalue())
        self.assertIsNone(resp["id"])
        self.assertEqual(resp["error"]["code"], mcp.PARSE_ERROR)
        resp = _rpc(self.fx, "no/such/method", {}, 3)
        self.assertEqual(resp["error"]["code"], mcp.METHOD_NOT_FOUND)
        resp = _rpc(self.fx, "tools/call", {"name": "nope", "arguments": {}}, 4)
        self.assertEqual(resp["error"]["code"], mcp.INVALID_PARAMS)
        resp = _rpc(self.fx, "tools/call", {"name": "task_start", "arguments": {}}, 5)
        self.assertEqual(resp["error"]["code"], mcp.INVALID_PARAMS)


class TaskToolTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.add_task("TASK-001-alpha")
        self.fx.add_task("TASK-002-beta", status="in-progress")
        self.fx.add_task("TASK-003-gamma", status="done")

    def tearDown(self):
        self.fx.cleanup()

    @staticmethod
    def _names(text):
        return set(re.findall(r"TASK-\d{3}-[a-z0-9-]+", text))

    def test_task_list_matches_cli(self):
        result = _call(self.fx, "task_list", {})
        self.assertFalse(result["isError"])
        # CLI 直调同一 fixture：同一 cmd_list 代码路径，输出表格文本一致 → 任务集一致
        mod = mcp.load_task_cli(_ROOT, str(self.fx.root))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.cmd_list([])
        self.assertEqual(self._names(result["content"][0]["text"]),
                         self._names(buf.getvalue()))
        self.assertEqual(self._names(result["content"][0]["text"]),
                         {"TASK-001-alpha", "TASK-002-beta", "TASK-003-gamma"})

    def test_task_list_status_filter(self):
        result = _call(self.fx, "task_list", {"status": "done"})
        self.assertEqual(self._names(result["content"][0]["text"]), {"TASK-003-gamma"})

    def test_task_start_sets_in_progress(self):
        result = _call(self.fx, "task_start", {"task_id": "TASK-001"})
        self.assertFalse(result["isError"], result["content"][0]["text"])
        mod = mcp.load_task_cli(_ROOT, str(self.fx.root))
        _, fm = mod.load_task(mod.find_task("TASK-001"))
        self.assertEqual(fm.get("metadata.status"), "in-progress")

    def test_task_done_happy_path(self):
        self.fx.add_task("TASK-010-close-ok", status="in-progress")  # in-progress → done 才是合法转换
        self.fx.add_verify("TASK-010")
        result = _call(self.fx, "task_done", {"task_id": "TASK-010"})
        self.assertFalse(result["isError"], result["content"][0]["text"])
        mod = mcp.load_task_cli(_ROOT, str(self.fx.root))
        _, fm = mod.load_task(mod.find_task("TASK-010"))
        self.assertEqual(fm.get("metadata.status"), "done")

    def test_task_done_p0_rejected_same_error_as_cli(self):
        # 治理闸门穿透：P0 无 approval-ref 经 MCP task_done 被拒，错误信息与
        # CLI 直调逐字一致（cmd_done = python3 kit/cli/task done 的同一代码路径）
        self.fx.add_task("TASK-090-p0-gate", status="in-progress", risk="P0")
        self.fx.add_verify("TASK-090")
        mod = mcp.load_task_cli(_ROOT, str(self.fx.root))
        err = io.StringIO()
        cli_code = 0
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                mod.cmd_done(["TASK-090"])
            except SystemExit as e:
                cli_code = e.code if isinstance(e.code, int) else 1
        result = _call(self.fx, "task_done", {"task_id": "TASK-090"})
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertEqual(cli_code, 1)
        self.assertEqual(text, err.getvalue().strip())  # 逐字一致
        self.assertIn("P0 任务缺少人工批准引用", text)
        _, fm = mod.load_task(mod.find_task("TASK-090"))
        self.assertEqual(fm.get("metadata.status"), "in-progress")  # 状态未被改动


class ContextAssembleTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.add_task("TASK-020-ctx")
        p = self.fx.root / "runtime" / "tasks" / "TASK-020-ctx.md"
        p.write_text(p.read_text(encoding="utf-8") + "\n涉及 runtime/notes.md\n",
                     encoding="utf-8")
        (self.fx.root / "runtime" / "notes.md").write_text("hello\n", encoding="utf-8")

    def tearDown(self):
        self.fx.cleanup()

    def test_assemble_renders_plan(self):
        result = _call(self.fx, "context_assemble", {"task_id": "TASK-020"})
        self.assertFalse(result["isError"], result["content"][0]["text"])
        text = result["content"][0]["text"]
        self.assertIn("# Context Plan — TASK-020", text)
        self.assertIn("runtime/tasks/TASK-020-ctx.md", text)

    def test_assemble_budget_passthrough(self):
        result = _call(self.fx, "context_assemble", {"task_id": "TASK-020", "budget": 50})
        self.assertIn("budget 50", result["content"][0]["text"])

    def test_assemble_missing_task(self):
        result = _call(self.fx, "context_assemble", {"task_id": "TASK-999"})
        self.assertTrue(result["isError"])
        self.assertIn("未找到任务", result["content"][0]["text"])


class StdlibOnlyTests(unittest.TestCase):
    """验收 5：kit/tools/mcp/*.py 行首 import 全部 stdlib（零第三方依赖）。"""

    def test_line_start_imports_are_stdlib(self):
        allowed = getattr(sys, "stdlib_module_names", None) or _FALLBACK_STDLIB
        mcp_dir = os.path.join(_ROOT, "kit", "tools", "mcp")
        pat = re.compile(r"^(import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)")
        checked = 0
        for fn in sorted(os.listdir(mcp_dir)):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(mcp_dir, fn), encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    m = pat.match(line)
                    if not m:
                        continue
                    checked += 1
                    top = m.group(2).split(".")[0]
                    self.assertIn(top, allowed, f"{fn}:{lineno} 行首 import 非 stdlib: {top}")
        self.assertGreaterEqual(checked, 8)  # server.py 顶层 stdlib import 数量下限


if __name__ == "__main__":
    unittest.main()
