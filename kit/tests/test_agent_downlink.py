#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_agent_downlink.py — 下行指令拾取/校验/执行/回报单测（TASK-036）。

覆盖验收标准（AGENT-DOWNLINK-CONTRACT v1.0 + TASK-036）：
- 正常执行（done/failed 回报、tail 脱敏与截断、argv 映射、cwd）
- 白名单外命令拒绝（agent 侧第二道闸，独立于 server）
- 白名单外项目拒绝（project_id ∉ agent.json projects）
- 执行中不丢拾取（R2-001：busy 跳过拾取调用，完成后下轮继续；心跳不阻塞）
- 执行超时 → failed(exit_code=-1)（SMELL-004 agent 侧自报）
- 幂等预检：任务已 done/in-review → skipped（契约 §三 三层②）
- pickup 401 → PushRejectedError；report 409 → already-terminal 幂等

用法: python -m unittest kit.tests.test_agent_downlink
"""
import http.server
import os
import subprocess
import tempfile
import threading
import types
import unittest

sys_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys  # noqa: E402
sys.path.insert(0, os.path.join(sys_path, "kit", "tools", "agent"))

import agent_downlink as dl  # noqa: E402
import agent_http  # noqa: E402


def _cmd(name="autoloop_coder", args=("TASK-009",), project="x1prototype", timeout=None,
         command_id=42):
    command = {"command_id": command_id, "seq": command_id,
               "dedup_key": f"{project}:TASK-009:coder",
               "project_id": project, "command": {"name": name, "args": list(args)},
               "created_by": "dispatcher"}
    if timeout is not None:
        command["timeout_secs"] = timeout
    return command


PROJECTS = [{"id": "x1prototype", "path": "/tmp/x1proto"},
            {"id": "westhill", "path": "/tmp/westhill"}]


def _runner(returncode=0, stdout="ok", stderr=""):
    def run(argv, cwd=None, capture_output=None, text=None, encoding=None,
            errors=None, timeout=None):
        run.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    run.calls = []
    return run


class ValidateTests(unittest.TestCase):
    def test_valid_command_returns_project(self):
        proj = dl.validate_command(_cmd(), PROJECTS)
        self.assertEqual(proj, {"id": "x1prototype", "path": "/tmp/x1proto"})

    def test_unknown_command_name_rejected(self):
        with self.assertRaises(dl.CommandRejected):
            dl.validate_command(_cmd(name="rm_rf"), PROJECTS)

    def test_project_outside_whitelist_rejected(self):
        with self.assertRaises(dl.CommandRejected):
            dl.validate_command(_cmd(project="other-proj"), PROJECTS)

    def test_args_must_be_string_array(self):
        with self.assertRaises(dl.CommandRejected):
            dl.validate_command(_cmd(args=[1, 2]), PROJECTS)

    def test_missing_inner_command_rejected(self):
        with self.assertRaises(dl.CommandRejected):
            dl.validate_command({"command_id": 1}, PROJECTS)


class ArgvTests(unittest.TestCase):
    def test_mapping_contract(self):
        self.assertEqual(dl.build_argv("task_start", ["TASK-009"], python="py"),
                         ["py", "kit/cli/task", "start", "TASK-009"])
        self.assertEqual(dl.build_argv("autoloop_coder", [], python="py"),
                         ["py", "kit/cli/autoloop", "coder", "--once"])
        self.assertEqual(dl.build_argv("autoloop_reviewer", [], python="py"),
                         ["py", "kit/cli/autoloop", "reviewer", "--once"])


class ScrubTests(unittest.TestCase):
    def test_secret_lines_removed(self):
        tail = "line1\nAuthorization: Bearer abc\ntoken=secret\nline4"
        self.assertEqual(dl.scrub_tail(tail), "line1\nline4")

    def test_tail_truncated_to_200_lines(self):
        tail = dl.scrub_tail("\n".join(f"l{i}" for i in range(300)))
        self.assertEqual(len(tail.splitlines()), 200)
        self.assertTrue(tail.splitlines()[0].startswith("l100"))


class ExecuteTests(unittest.TestCase):
    def test_normal_execution_done_report(self):
        runner = _runner(returncode=0, stdout="out1\nAuthorization: Bearer x\nout3",
                         stderr="err1")
        report = dl.execute_command(_cmd(), PROJECTS[0], python="py", runner=runner)
        self.assertEqual(report["status"], "done")
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(report["stdout_tail"], "out1\nout3")  # 脱敏生效
        self.assertTrue(report["finished_at"].endswith("Z"))
        self.assertEqual(runner.calls[0]["cwd"], "/tmp/x1proto")
        self.assertEqual(runner.calls[0]["timeout"], 1800)

    def test_nonzero_exit_failed_report(self):
        report = dl.execute_command(_cmd(), PROJECTS[0], runner=_runner(returncode=3))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["exit_code"], 3)

    def test_execution_timeout_reports_failed_minus1(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout"))
        report = dl.execute_command(_cmd(), PROJECTS[0], runner=run)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["exit_code"], -1)
        self.assertIn("timeout", report["stderr_tail"])

    def test_precheck_task_done_reports_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "runtime", "tasks")
            os.makedirs(tasks)
            with open(os.path.join(tasks, "TASK-009-demo.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: TASK-009-demo\nmetadata:\n  status: done\n---\n")
            runner = _runner()
            report = dl.execute_command(_cmd(args=["TASK-009"]), {"id": "p", "path": tmp},
                                        python="py", runner=runner)
            self.assertEqual(report["status"], "skipped")
            self.assertEqual(runner.calls, [])  # 未执行子进程

    def test_precheck_task_in_review_reports_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "runtime", "tasks")
            os.makedirs(tasks)
            with open(os.path.join(tasks, "TASK-009-demo.md"), "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  status: in-review\n---\n")
            report = dl.execute_command(_cmd(args=["TASK-009"]), {"id": "p", "path": tmp},
                                        runner=_runner())
            self.assertEqual(report["status"], "skipped")

    def test_precheck_open_task_still_executes(self):
        with tempfile.TemporaryDirectory() as tmp:
            tasks = os.path.join(tmp, "runtime", "tasks")
            os.makedirs(tasks)
            with open(os.path.join(tasks, "TASK-009-demo.md"), "w", encoding="utf-8") as f:
                f.write("---\nmetadata:\n  status: open\n---\n")
            runner = _runner()
            report = dl.execute_command(_cmd(args=["TASK-009"]), {"id": "p", "path": tmp},
                                        python="py", runner=runner)
            self.assertEqual(report["status"], "done")
            self.assertEqual(len(runner.calls), 1)


class _WorkerHarness:
    """DownlinkWorker 测试台：可编程 pickup/execute/report 替身 + 可控执行线程。"""

    def __init__(self, commands, execute_gate=None, **overrides):
        import io
        self.pickups = list(commands)
        self.picked = []
        self.reports = []
        self.execute_calls = []
        self.execute_gate = execute_gate  # threading.Event 或 None（立即完成）
        from agent_loop import AgentLog
        self.log = AgentLog(quiet=True, stream=io.StringIO(), err_stream=io.StringIO())
        self._thread_holder = []

        def pickup(url, token):
            self.picked.append(url)
            return self.pickups.pop(0) if self.pickups else None

        def execute(command, project):
            self.execute_calls.append((command["command_id"], project["id"]))
            if self.execute_gate is not None:
                self.execute_gate.wait(timeout=5)
            return {"status": "done", "exit_code": 0, "stdout_tail": "ok",
                    "stderr_tail": "", "finished_at": "2026-08-30T00:00:00Z"}

        def report(url, token, cid, body):
            self.reports.append((cid, body["status"]))
            return {"command_id": cid, "status": body["status"]}

        def spawner(target):
            t = threading.Thread(target=target, daemon=True)
            self._thread_holder.append(t)
            return t

        kw = dict(pickup_fn=pickup, execute_fn=execute, report_fn=report, spawner=spawner)
        kw.update(overrides)
        cfg = {"server_url": "http://hub.example", "token": "tok", "projects": PROJECTS}
        self.worker = dl.DownlinkWorker(cfg, self.log, **kw)

    def join(self):
        for t in self._thread_holder:
            t.join(timeout=5)


class WorkerTests(unittest.TestCase):
    def test_dispatch_and_report_happy_path(self):
        h = _WorkerHarness([_cmd()])
        self.assertEqual(h.worker.tick(), "dispatched")
        h.join()
        self.assertEqual(h.reports, [(42, "done")])

    def test_busy_skips_pickup_r2_001(self):
        gate = threading.Event()
        h = _WorkerHarness([_cmd(command_id=1), _cmd(command_id=2)], execute_gate=gate)
        self.assertEqual(h.worker.tick(), "dispatched")
        picked_after_first = len(h.picked)
        self.assertEqual(h.worker.tick(), "busy")       # 执行中：不拾取
        self.assertEqual(h.worker.tick(), "busy")
        self.assertEqual(len(h.picked), picked_after_first)  # 拾取调用零增长
        gate.set()
        h.join()
        self.assertEqual(h.worker.tick(), "dispatched")  # 完成后下轮继续拾取（不丢）
        h.join()
        self.assertEqual([r[0] for r in h.reports], [1, 2])

    def test_rejected_command_reported_failed(self):
        h = _WorkerHarness([_cmd(name="evil_cmd")])
        self.assertEqual(h.worker.tick(), "rejected")
        self.assertEqual(h.reports, [(42, "failed")])
        # 拒绝后单槽释放，可继续拾取
        self.assertEqual(h.worker.tick(), "empty")

    def test_rejected_project_reported_failed(self):
        h = _WorkerHarness([_cmd(project="not-mine")])
        self.assertEqual(h.worker.tick(), "rejected")
        self.assertEqual(h.reports, [(42, "failed")])

    def test_pickup_error_releases_slot(self):
        h = _WorkerHarness([])
        def boom(url, token):
            raise agent_http.PushNetworkError("conn refused")
        h.worker._pickup = boom
        self.assertEqual(h.worker.tick(), "pickup-error")
        self.assertEqual(h.worker.tick(), "pickup-error")  # 槽已释放，可重试

    def test_empty_then_dispatch(self):
        h = _WorkerHarness([None, _cmd()])
        self.assertEqual(h.worker.tick(), "empty")
        self.assertEqual(h.worker.tick(), "dispatched")
        h.join()
        self.assertEqual(h.reports, [(42, "done")])


class HttpTests(unittest.TestCase):
    """HTTP 层薄测：URL/方法/鉴权头正确 + 401/409 语义（用本地 stub HTTP server）。"""

    def _serve(self, handler):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return "http://127.0.0.1:%d" % server.server_address[1]

    def test_pickup_success_returns_command(self):
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):  # noqa: E306
            def do_GET(self):
                seen["path"] = self.path
                seen["auth"] = self.headers.get("Authorization")
                body = b'{"command": {"command_id": 7}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self._serve(H)
        cmd = dl.pickup_command(base, "tok-1")
        self.assertEqual(cmd, {"command_id": 7})
        self.assertEqual(seen["path"], "/api/downlink/pickup")
        self.assertEqual(seen["auth"], "Bearer tok-1")

    def test_pickup_401_raises_rejected(self):
        class H(http.server.BaseHTTPRequestHandler):  # noqa: E306
            def do_GET(self):
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        base = self._serve(H)
        with self.assertRaises(agent_http.PushRejectedError) as cm:
            dl.pickup_command(base, "bad")
        self.assertEqual(cm.exception.status, 401)

    def test_pickup_empty_queue_returns_none(self):
        class H(http.server.BaseHTTPRequestHandler):  # noqa: E306
            def do_GET(self):
                body = b'{"command": null}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.assertIsNone(dl.pickup_command(self._serve(H), "tok"))

    def test_report_success_and_409(self):
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):  # noqa: E306
            def do_POST(self):
                seen["path"] = self.path
                length = int(self.headers.get("Content-Length", 0))
                seen["body"] = self.rfile.read(length).decode("utf-8")
                if '"done"' in seen["body"]:
                    body = b'{"command_id": 42, "status": "done"}'
                    self.send_response(200)
                else:
                    body = b'{"error": "already"}'
                    self.send_response(409)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        base = self._serve(H)
        out = dl.report_result(base, "tok", 42, {"status": "done", "exit_code": 0,
                                                 "stdout_tail": "", "stderr_tail": "",
                                                 "finished_at": "Z"})
        self.assertEqual(seen["path"], "/api/downlink/commands/42/result")
        self.assertEqual(out, {"command_id": 42, "status": "done"})
        out = dl.report_result(base, "tok", 42, {"status": "failed", "exit_code": 1,
                                                 "stdout_tail": "", "stderr_tail": "",
                                                 "finished_at": "Z"})
        self.assertEqual(out, "already-terminal")  # 409 幂等忽略


if __name__ == "__main__":
    unittest.main()
