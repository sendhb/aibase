"""TASK-027 — autoloop Python 三件套单测（TASK-023/024/025 交付）。

覆盖：
- autoloop_coder：parse_args 默认值/env 覆盖、build_prompt、run_once 治理分支
  （no_task / P0 block / rework block / open 触发 start / in-progress 不重复 start /
   assignee 写入含反斜杠 / LLM 退出码透传）、run_llm_coder 事件（mock argv）
- autoloop_reviewer：parse_args、pick_in_review（生成者≠审查者 / fast-path 跳过）、
  run_once（no_task / 六维 / 三问）、run_llm_reviewer 事件
- autoloop_launcher：parse_args / build_role_args / both_interval / _lock_probe
  （含跨进程持锁）/ cmd_stop（无实例 / 过期 PID）/ usage / 子进程 e2e
  （AUTOLOOP_LIB_DIR stub 核心 + AIOS_PROJECT_ROOT 临时根：coder --once argv 透传、
   both --once coder→reviewer 顺序、both --interval 后台化+PID+stop、非法参数拒绝）

**安全边界**：绝不调用 coder/reviewer 的 main()——其内部 find_project_root 恒定位
到真实仓库，会 pick 真实 open 任务并可能触发真实 LLM。核心层测试只走 run_once
注入 seam；e2e 通过 AUTOLOOP_LIB_DIR（stub 核心）+ AIOS_PROJECT_ROOT（临时根）隔离。
"""
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_KIT)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import autoloop_coder as C  # noqa: E402
import autoloop_launcher as L  # noqa: E402
import autoloop_reviewer as R  # noqa: E402
import events  # noqa: E402
import llm  # noqa: E402
import tasklib  # noqa: E402

PY = sys.executable
LAUNCHER = os.path.join(_REPO, "kit", "cli", "autoloop")

STUB_CORE = """import os, sys
with open(os.path.join(os.getcwd(), "stub_calls.txt"), "a", encoding="utf-8") as f:
    f.write("%(role)s " + " ".join(sys.argv[1:]) + "\\n")
"""


def _write_task(tasks_dir, name, status="open", risk="P2", priority="P2",
                reviewer="any", approval="none", rework="0", assignee="any",
                updated=None):
    os.makedirs(tasks_dir, exist_ok=True)
    path = os.path.join(tasks_dir, name + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "---\nname: %s\ndescription: t\nmetadata:\n  type: task\n"
            "  status: %s\n  priority: %s\n  risk: %s\n  approval-ref: %s\n"
            "  assignee: %s\n  reviewer: %s\n  rework-count: %s\n"
            "  depends-on: []\n%s---\n\n- [x] ok\n" % (
                name, status, priority, risk, approval, assignee, reviewer, rework,
                ("  updated: %s\n" % updated) if updated else ""))
    return path


def _outcomes(log_dir, name):
    path = events.events_path(log_dir, name)
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(l)["outcome"] for l in f if l.strip()]


class _RootCase(unittest.TestCase):
    """临时项目根（aios.config.yaml + runtime 目录）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        with open(os.path.join(self.root, "aios.config.yaml"), "w") as f:
            f.write("commands: {}\n")
        self.tasks = os.path.join(self.root, "runtime", "tasks")
        self.logs = os.path.join(self.root, "runtime", "logs")
        os.makedirs(self.tasks)
        os.makedirs(self.logs)


class CoderArgsTests(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            o = C.parse_args([])
        self.assertEqual((o.interval, o.once, o.timeout, o.llm, o.id),
                         (300, False, 1800, "pi", "autoloop-coder"))

    def test_env_overrides(self):
        with mock.patch.dict(os.environ, {"AIOS_LLM": "deepseek",
                                          "AUTOLOOP_CODER_ID": "agent-7"}, clear=True):
            o = C.parse_args([])
        self.assertEqual((o.llm, o.id), ("deepseek", "agent-7"))

    def test_flags(self):
        o = C.parse_args(["--once", "--llm", "claude", "--unattended",
                          "--interval", "5", "--timeout", "9"])
        self.assertEqual((o.once, o.llm, o.unattended, o.interval, o.timeout),
                         (True, "claude", True, 5, 9))


class CoderPromptTests(unittest.TestCase):
    def test_fast_path_prompt(self):
        p = C.build_prompt("TASK-901", "TASK-901-x.md", True, "done")
        self.assertIn("TASK-901", p)
        self.assertIn("task done TASK-901", p)

    def test_review_path_prompt(self):
        p = C.build_prompt("TASK-901", "TASK-901-x.md", False, "review")
        self.assertIn("task review TASK-901", p)
        self.assertNotIn("task done TASK-901", p)


class CoderRunOnceTests(_RootCase):
    def _opts(self, extra=None):
        return C.parse_args(["--once", "--id", "agent-x", "--llm", "claude"]
                            + (extra or []))

    def _run(self, opts, task_cli, llm):
        return C.run_once(self.root, opts, None, self.logs,
                          task_cli_fn=task_cli, llm_fn=llm)

    def test_no_task(self):
        calls, llm_calls = [], []
        rc = self._run(self._opts(), lambda root, *a: calls.append(a) or 0,
                       lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual((rc, calls, llm_calls), (0, [], []))
        self.assertEqual(_outcomes(self.logs, "coder"), ["no_task"])

    def test_p0_blocked_no_llm(self):
        _write_task(self.tasks, "TASK-902-p0", risk="P0", approval="none")
        calls, llm_calls = [], []
        rc = self._run(self._opts(),
                       lambda root, *a: calls.append(a) or 0,
                       lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "block")
        self.assertEqual(llm_calls, [])
        self.assertEqual(_outcomes(self.logs, "coder"), ["blocked_p0"])

    def test_rework_exceeded_no_llm(self):
        _write_task(self.tasks, "TASK-903-rw", rework="3")
        calls, llm_calls = [], []
        self._run(self._opts(),
                  lambda root, *a: calls.append(a) or 0,
                  lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual(calls[0][0], "block")
        self.assertEqual(llm_calls, [])
        self.assertEqual(_outcomes(self.logs, "coder"), ["blocked_rework"])

    def test_open_task_start_assignee_llm(self):
        _write_task(self.tasks, "TASK-901-fast")
        calls, llm_calls = [], []
        rc = self._run(self._opts(),
                       lambda root, *a: calls.append(a) or 0,
                       lambda *a, **k: llm_calls.append((a, k)) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][:2], ("start", "TASK-901"))
        self.assertEqual(len(llm_calls), 1)
        provider, prompt = llm_calls[0][0]
        self.assertEqual((provider, llm_calls[0][1]["task"]), ("claude", "TASK-901"))
        self.assertIn("done", prompt)
        _, fm = __import__("tasklib").load_task(
            os.path.join(self.tasks, "TASK-901-fast.md"))
        self.assertEqual(fm.get("metadata.assignee"), "agent-x")

    def test_in_progress_no_restart(self):
        _write_task(self.tasks, "TASK-904-ip", status="in-progress")
        calls, _ = [], []
        self._run(self._opts(), lambda root, *a: calls.append(a) or 0,
                  lambda *a, **k: 0)
        self.assertNotIn("start", [c[0] for c in calls])

    def test_llm_rc_passthrough(self):
        _write_task(self.tasks, "TASK-901-fast")
        rc = self._run(self._opts(), lambda root, *a: 0, lambda *a, **k: 124)
        self.assertEqual(rc, 124)

    def test_assignee_backslash_value(self):
        _write_task(self.tasks, "TASK-905-bs")
        opts = C.parse_args(["--once", "--id", r"back\slash"])
        self._run(opts, lambda root, *a: 0, lambda *a, **k: 0)
        with open(os.path.join(self.tasks, "TASK-905-bs.md"), encoding="utf-8") as f:
            self.assertIn(r"assignee: back\slash", f.read())


class CoderLlmEventTests(_RootCase):
    def _run(self, code):
        argv = [PY, "-c", code]
        with mock.patch.object(C, "_resolve_provider_argv",
                               return_value=(argv, None)):
            return C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901")

    def test_ok_event(self):
        self.assertEqual(self._run(""), 0)
        self.assertEqual(_outcomes(self.logs, "coder"), ["ok"])

    def test_error_event(self):
        self.assertEqual(self._run("import sys; sys.exit(3)"), 3)
        self.assertEqual(_outcomes(self.logs, "coder"), ["error"])

    def test_timeout_event(self):
        with mock.patch.object(C, "_resolve_provider_argv",
                               return_value=([PY, "-c", "import time; time.sleep(30)"],
                                             None)):
            rc = C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901",
                                 timeout=1)
        self.assertEqual(rc, 124)
        self.assertEqual(_outcomes(self.logs, "coder"), ["timeout"])


class ReviewerTests(_RootCase):
    def test_defaults_and_env(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            o = R.parse_args([])
        self.assertEqual((o.id, o.interval, o.once), ("autoloop-reviewer", 300, False))
        with mock.patch.dict(os.environ, {"AUTOLOOP_REVIEWER_ID": "rev-9"}, clear=True):
            self.assertEqual(R.parse_args([]).id, "rev-9")

    def test_pick_skips_self_and_picks_other(self):
        _write_task(self.tasks, "TASK-901-mine", status="in-review",
                    risk="P1", assignee="reviewer-y")
        _write_task(self.tasks, "TASK-902-other", status="in-review",
                    risk="P1", assignee="agent-x")
        self.assertEqual(R.pick_in_review(self.tasks, "reviewer-y"), "TASK-902")

    def test_pick_skips_fast_path(self):
        _write_task(self.tasks, "TASK-901-fastish", status="in-review",
                    risk="P2", reviewer="any")
        self.assertEqual(R.pick_in_review(self.tasks, "reviewer-y"), None)

    def test_pick_emits_skip_fastpath_event(self):
        # TASK-108 验收：fast-path 跳过不再静默——autoloop-reviewer-events.jsonl
        # 新增含该 task id 的告警事件 + 审计流新增 reviewer.fastpath_skip（带 reason）
        _write_task(self.tasks, "TASK-905-stuck", status="in-review",
                    risk="P2", reviewer="any")
        self.assertEqual(
            R.pick_in_review(self.tasks, "reviewer-y", log_dir=self.logs), None)
        outcomes = _outcomes(self.logs, "reviewer")
        self.assertEqual(outcomes, ["skip_fastpath"])
        with open(events.events_path(self.logs, "reviewer"), encoding="utf-8") as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["task"], "TASK-905")  # 短 id
        self.assertIn("fast-path", rec["reason"])
        # 审计 outbox：新增事件类型 reviewer.fastpath_skip，带 task id 与 reason
        with open(tasklib.events_path(self.logs), encoding="utf-8") as f:
            audit = [json.loads(l) for l in f if l.strip()]
        skip = [e for e in audit if e["ev"] == "reviewer.fastpath_skip"]
        self.assertEqual(len(skip), 1)
        self.assertEqual((skip[0]["task"], bool(skip[0]["reason"])),
                         ("TASK-905", True))
        self.assertEqual(tasklib.validate_events(self.logs), (0, 1))

    def test_run_once_emits_stale_in_review(self):
        # TASK-108 验收：构造 in-review 超阈值任务（updated 3 天前 ≈ --hours 0
        # 等价注入）→ 循环日志含 stale 告警 + 事件；滞留 fast-path 仍不触发 LLM
        old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        _write_task(self.tasks, "TASK-906-limbo", status="in-review",
                    risk="P2", reviewer="any", updated=old)
        R._stale_rounds = 0  # 首轮即扫
        llm_calls = []
        log_path = os.path.join(self.logs, "reviewer-stale-hit.log")
        rc = R.run_once(self.root, R.parse_args(["--once", "--id", "reviewer-y"]),
                        log_path, self.logs, task_cli_fn=lambda root, *a: 0,
                        llm_fn=lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual((rc, llm_calls), (0, []))
        self.assertEqual(_outcomes(self.logs, "reviewer"),
                         ["stale_in_review", "skip_fastpath", "no_task"])
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("⚠ TASK-906 滞留 in-review", text)  # 日志含 stale 告警
        self.assertIn("⚠ TASK-906 是 fast-path", text)    # 日志含 fast-path 告警

    def test_stale_scan_every_n_rounds(self):
        # 计划.3：每 STALE_EVERY_ROUNDS 轮巡检一次（首轮即扫）；skip_fastpath
        # 逐轮告警不受 N 门控，stale 事件只在扫描轮出现
        old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        _write_task(self.tasks, "TASK-908-gate", status="in-review",
                    risk="P2", reviewer="someone", assignee="agent-x",
                    updated=old)
        R._stale_rounds = 0
        run = lambda: R.run_once(self.root,
                                 R.parse_args(["--once", "--id", "reviewer-y"]),
                                 None, self.logs, task_cli_fn=lambda root, *a: 0,
                                 llm_fn=lambda *a, **k: 0)
        run()  # 第 1 轮：扫描
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["stale_in_review"])
        run()  # 第 2 轮：非扫描轮 → 无 stale 事件
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["stale_in_review"])

    def test_run_once_zero_noise_when_no_stale(self):
        # TASK-108 验收：无滞留任务时一轮循环日志无 stale/skip 输出（零噪音）
        _write_task(self.tasks, "TASK-909-fresh", status="in-review",
                    risk="P2", reviewer="someone", assignee="agent-x")
        R._stale_rounds = 0
        log_path = os.path.join(self.logs, "reviewer-zero-noise.log")
        R.run_once(self.root, R.parse_args(["--once", "--id", "reviewer-y"]),
                   log_path, self.logs, task_cli_fn=lambda root, *a: 0,
                   llm_fn=lambda *a, **k: 0)
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("stale", text)
        self.assertNotIn("skip_fastpath", text)
        self.assertEqual(_outcomes(self.logs, "reviewer"), [])

    def test_run_once_stale_non_fastpath_still_picked(self):
        # 滞留巡检是旁路：非 fast-path 滞留任务仍照常被选中审查
        old = (datetime.date.today() - datetime.timedelta(days=3)).isoformat()
        _write_task(self.tasks, "TASK-907-old", status="in-review",
                    risk="P2", reviewer="someone", assignee="agent-x",
                    updated=old)
        R._stale_rounds = 0
        llm_calls = []
        R.run_once(self.root, R.parse_args(["--once", "--id", "reviewer-y"]),
                   None, self.logs, task_cli_fn=lambda root, *a: 0,
                   llm_fn=lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual(len(llm_calls), 1)
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["stale_in_review"])

    def test_pick_none_when_empty(self):
        self.assertEqual(R.pick_in_review(self.tasks, "reviewer-y"), None)

    def test_run_once_no_task(self):
        llm_calls = []
        rc = R.run_once(self.root, R.parse_args(["--once"]), None, self.logs,
                        task_cli_fn=lambda root, *a: 0,
                        llm_fn=lambda *a, **k: llm_calls.append(a) or 0)
        self.assertEqual((rc, llm_calls), (0, []))
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["no_task"])
        self.assertTrue(os.path.isfile(
            os.path.join(self.logs, "autoloop-reviewer.heartbeat")))

    def test_run_once_p1_six_dim(self):
        _write_task(self.tasks, "TASK-901-big", status="in-review",
                    risk="P1", assignee="agent-x")
        llm_calls = []
        rc = R.run_once(self.root, R.parse_args(["--once", "--id", "reviewer-y"]),
                        None, self.logs, task_cli_fn=lambda root, *a: 0,
                        llm_fn=lambda *a, **k: llm_calls.append((a, k)) or 0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(llm_calls), 1)
        self.assertIn("六维", llm_calls[0][0][1])
        self.assertEqual(llm_calls[0][1]["task"], "TASK-901")

    def test_run_once_p2_three_questions(self):
        # P2 但指定 reviewer（非 fast-path）→ 三问
        _write_task(self.tasks, "TASK-902-small", status="in-review",
                    risk="P2", reviewer="someone", assignee="agent-x")
        llm_calls = []
        R.run_once(self.root, R.parse_args(["--once", "--id", "reviewer-y"]),
                   None, self.logs, task_cli_fn=lambda root, *a: 0,
                   llm_fn=lambda *a, **k: llm_calls.append(a) or 0)
        self.assertIn("三问", llm_calls[0][1])

    def test_run_llm_reviewer_event_file(self):
        with mock.patch.object(R, "_resolve_provider_argv",
                               return_value=([PY, "-c", ""], None)):
            rc = R.run_llm_reviewer("pi", "P", log_dir=self.logs, task="TASK-901")
        self.assertEqual((rc, _outcomes(self.logs, "reviewer")), (0, ["ok"]))


class LauncherUnitTests(_RootCase):
    def test_parse_args_defaults(self):
        p = L.parse_args(["coder"])
        self.assertEqual((p.mode, p.once, p.interval, p.foreground), ("coder", False, None, False))

    def test_parse_args_both_full(self):
        p = L.parse_args(["both", "--once", "--coder-llm", "deepseek",
                          "--reviewer-llm", "claude", "--interval", "300"])
        self.assertEqual((p.once, p.coder_llm, p.reviewer_llm, p.interval),
                         (True, "deepseek", "claude", 300))

    def test_parse_args_invalid(self):
        self.assertEqual(L.parse_args([]), None)
        self.assertEqual(L.parse_args(["coder", "--interval"]), None)
        self.assertEqual(L.parse_args(["coder", "--interval", "abc"]), None)

    def test_build_role_args(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            p = L.parse_args(["coder"])
            self.assertEqual(L.build_role_args(p, None, once=None),
                             ["--once", "--llm", "pi"])
            p2 = L.parse_args(["coder", "--id", "A", "--timeout", "9",
                               "--unattended", "extra"])
            self.assertEqual(L.build_role_args(p2, None, once=False),
                             ["--interval", "30", "--id", "A", "--timeout", "9",
                              "--unattended", "--llm", "pi", "extra"])
            self.assertEqual(L.build_role_args(p, "deepseek", once=True),
                             ["--once", "--llm", "deepseek"])

    def test_both_interval_priority(self):
        p = L.parse_args(["both", "--interval", "60"])
        self.assertEqual(L.both_interval(p), 60)
        with mock.patch.dict(os.environ, {"AUTOLOOP_DEFAULT_INTERVAL": "7"}):
            self.assertEqual(L.both_interval(L.parse_args(["both"])), 7)
        with mock.patch.dict(os.environ, {"AUTOLOOP_DEFAULT_INTERVAL": "x"}):
            self.assertEqual(L.both_interval(L.parse_args(["both"])), 30)

    def test_lock_probe_free_and_held(self):
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)  # 生产契约：调用方保证锁目录存在
        lockfile = os.path.join(lock_dir, "probe.lock")
        self.assertTrue(L._lock_probe(lockfile))
        child = subprocess.Popen(
            [PY, "-c",
             "import sys, time; sys.path.insert(0, %r); import lock; "
             "fd = lock._open_lock(sys.argv[1]); lock._acquire(fd); time.sleep(15)"
             % _LIB, lockfile])
        try:
            deadline = time.time() + 10
            held = True
            while time.time() < deadline:
                if not L._lock_probe(lockfile):
                    held = False
                    break
                time.sleep(0.2)
            self.assertFalse(held, "子进程持锁后 probe 应为 False")
        finally:
            child.terminate()
            child.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline and not L._lock_probe(lockfile):
            time.sleep(0.2)
        self.assertTrue(L._lock_probe(lockfile), "子进程退出后锁应释放")

    def test_cmd_stop_no_instance(self):
        self.assertEqual(L.cmd_stop(self.root), 0)

    def test_cmd_stop_stale_pid(self):
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        pidfile = os.path.join(lock_dir, L.BOTH_PID)
        with open(pidfile, "w", encoding="ascii") as f:
            f.write("999999999")
        self.assertEqual(L.cmd_stop(self.root), 0)
        self.assertFalse(os.path.isfile(pidfile))

    def test_usage_and_main_rejects(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(L.usage(), 1)
            self.assertEqual(L.main([]), 1)
            self.assertEqual(L.main(["wat"]), 1)
            self.assertEqual(L.main(["coder", "--foreground"]), 1)
            self.assertEqual(L.main(["coder", "--coder-llm", "pi"]), 1)


class FatalScanTests(unittest.TestCase):
    """llm.scan_fatal_output 纯函数（TASK-107 P1-1）：宁可漏判，不可误杀。"""

    def test_hits(self):
        for text in ("gateway_error: quota exceeded",
                     "Error 403004: API key suspended",
                     "account in arrears, please top up",
                     "账户欠费，请充值后重试",
                     "request failed: HTTP 401",
                     "http/1.1 403",
                     "Status Code 401 Unauthorized",
                     "403 Forbidden"):
            self.assertIsNotNone(llm.scan_fatal_output(text), text)

    def test_clean_output_not_flagged(self):
        for text in ("", "all good", "TASK-123 done", "error 500, retrying",
                     "rate limited 429, backoff", "line 4030 of file.py"):
            self.assertIsNone(llm.scan_fatal_output(text), text)


class FatalFastFailTests(_RootCase):
    """stub provider 输出网关错误 → 会话秒级终止 + error 事件（TASK-107 P1-1）。"""

    FATAL_STUB = ("print('gateway_error 403004 in arrears', flush=True); "
                  "import time; time.sleep(60)")

    def _run_coder(self, code, timeout):
        with mock.patch.object(C, "_resolve_provider_argv",
                               return_value=([PY, "-c", code], None)):
            return C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901",
                                   timeout=timeout)

    def test_fatal_terminates_fast_with_error_event(self):
        t0 = time.monotonic()
        rc = self._run_coder(self.FATAL_STUB, timeout=60)
        elapsed = time.monotonic() - t0
        self.assertEqual(rc, llm.FATAL_EXIT)
        self.assertLess(elapsed, 30, "应秒级终止，而非拖满 timeout")
        self.assertEqual(_outcomes(self.logs, "coder"), ["error"])

    def test_timeout_semantics_unchanged(self):
        rc = self._run_coder("import time; time.sleep(30)", timeout=2)
        self.assertEqual(rc, 124)
        self.assertEqual(_outcomes(self.logs, "coder"), ["timeout"])

    def test_reviewer_fatal_fast_fail(self):
        with mock.patch.object(R, "_resolve_provider_argv",
                               return_value=([PY, "-c", self.FATAL_STUB], None)):
            rc = R.run_llm_reviewer("pi", "P", log_dir=self.logs,
                                    task="TASK-901", timeout=60)
        self.assertEqual(rc, llm.FATAL_EXIT)
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["error"])

    # ---- 2026-09-06 事故回归：fatal 扫描只认本轮新增字节 ----
    # per-task log 跨轮累积，历史轮的 mock 测试文本（"HTTP 401"）与 LLM
    # 应答任务正文的 "欠费" 等留在文件尾部，全文件尾部扫描会秒杀后续
    # 每一轮会话（TASK-107 被自家卷宗卡死 ~2.2h 实录）。

    POLLUTED_LOG = "历史 mock: 推送失败（HTTP 401）\n历史应答: 账户欠费\n" * 40

    def _prewrite_task_log(self, role_dir_flag=None):
        tpath = os.path.join(self.logs, "tasks", "TASK-901.log")
        os.makedirs(os.path.dirname(tpath), exist_ok=True)
        with open(tpath, "w", encoding="utf-8") as f:
            f.write(self.POLLUTED_LOG)
        return tpath

    def test_polluted_history_bytes_not_fatal_coder(self):
        self._prewrite_task_log()
        rc = self._run_coder("print('clean output'); import sys; sys.exit(0)",
                             timeout=30)
        self.assertEqual(rc, 0, "历史字节不得误杀本轮干净会话")
        self.assertEqual(_outcomes(self.logs, "coder"), ["ok"])

    def test_polluted_history_bytes_not_fatal_reviewer(self):
        self._prewrite_task_log()
        with mock.patch.object(R, "_resolve_provider_argv",
                               return_value=([PY, "-c",
                                              "print('clean'); import sys; sys.exit(0)"],
                                             None)):
            rc = R.run_llm_reviewer("pi", "P", log_dir=self.logs,
                                    task="TASK-901", timeout=30)
        self.assertEqual(rc, 0)
        self.assertEqual(_outcomes(self.logs, "reviewer"), ["ok"])

    def test_new_bytes_still_fatal(self):
        """偏移语义不漏判：本轮新增字节命中特征词仍秒级终止。"""
        rc = self._run_coder("print('HTTP 401 from this round', flush=True); "
                             "import time; time.sleep(60)", timeout=60)
        self.assertEqual(rc, llm.FATAL_EXIT)

    def test_tail_text_from_scopes_offset(self):
        p = os.path.join(self.logs, "tail_from.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("http 401 old" * 100)
        size = os.path.getsize(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write("clean new bytes")
        self.assertEqual(C._tail_text_from(p, 8192, size), "clean new bytes")
        self.assertEqual(C._tail_text_from(p, 8192, size + 100), "")
        # 全文件尾部会误判（旧行为）；偏移语义不误判
        self.assertIsNotNone(llm.scan_fatal_output(C._tail_text(p, 8192)))
        self.assertIsNone(llm.scan_fatal_output(C._tail_text_from(p, 8192, size)))


class EnsureUnitTests(_RootCase):
    """ensure 纯函数与参数解析（TASK-107 P0-1）。"""

    def test_ensure_threshold(self):
        self.assertEqual(L.ensure_threshold(30, 1800), 1860)
        self.assertEqual(L.ensure_threshold(300, 600), 1200)

    def test_ensure_decision_branches(self):
        # 壳不在（PID 缺失/已死）→ spawn
        self.assertEqual(L.ensure_decision(False, None, 100), ("spawn", "pid-dead"))
        # 壳在 + 心跳停滞 → restart
        self.assertEqual(L.ensure_decision(True, 150, 100)[0], "restart")
        # 壳在 + 心跳新鲜 / 从未写心跳（进程太新防误杀）→ noop
        self.assertEqual(L.ensure_decision(True, 50, 100), ("noop", "healthy"))
        self.assertEqual(L.ensure_decision(True, None, 100), ("noop", "healthy"))

    def test_parse_args_max_age(self):
        p = L.parse_args(["ensure", "--max-age", "60"])
        self.assertEqual((p.mode, p.max_age), ("ensure", 60))
        self.assertIsNone(L.parse_args(["ensure", "--max-age"]), None)
        self.assertIsNone(L.parse_args(["ensure", "--max-age", "abc"]), None)

    def test_cmd_ensure_rejects_bad_max_age(self):
        p = L.parse_args(["ensure", "--max-age", "0"])
        self.assertEqual(L.cmd_ensure(self.root, p), 1)


class WatchdogUnitTests(_RootCase):
    """watchdog 薄壳单测（TASK-111）：调度/心跳/事件，判定零复制。"""

    def test_watchdog_interval_priority(self):
        p = L.parse_args(["watchdog", "--interval", "60"])
        self.assertEqual(L.watchdog_interval(p), 60)
        with mock.patch.dict(os.environ, {"AUTOLOOP_WATCHDOG_INTERVAL": "7"}):
            self.assertEqual(L.watchdog_interval(L.parse_args(["watchdog"])), 7)
        with mock.patch.dict(os.environ, {"AUTOLOOP_WATCHDOG_INTERVAL": "x"}):
            self.assertEqual(L.watchdog_interval(L.parse_args(["watchdog"])), 120)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(L.watchdog_interval(L.parse_args(["watchdog"])), 120)

    def test_run_ensure_invokes_launcher_ensure(self):
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

        with mock.patch.object(L.subprocess, "run", fake_run):
            ok, detail = L.run_ensure(self.root, max_age=60)
        self.assertTrue(ok)
        self.assertEqual(seen["argv"][:3],
                         [sys.executable, L.LAUNCHER_SCRIPT, "ensure"])
        self.assertEqual(seen["argv"][-2:], ["--max-age", "60"])
        self.assertEqual(seen["kw"]["cwd"], self.root)
        self.assertLessEqual(seen["kw"]["timeout"], L.ENSURE_TIMEOUT)
        self.assertIn("rc=0", detail)

    def test_run_ensure_timeout_tolerated(self):
        def _boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="ensure", timeout=L.ENSURE_TIMEOUT)

        with mock.patch.object(L.subprocess, "run", _boom):
            ok, detail = L.run_ensure(self.root)
        self.assertFalse(ok)
        self.assertIn("超时", detail)

    def test_run_ensure_oserror_tolerated(self):
        def _boom(*a, **kw):
            raise OSError("no such script")

        with mock.patch.object(L.subprocess, "run", _boom):
            ok, detail = L.run_ensure(self.root)
        self.assertFalse(ok)
        self.assertIn("启动失败", detail)

    def test_watchdog_once_ok_writes_hb_and_event(self):
        p = L.parse_args(["watchdog", "--once"])
        with mock.patch.object(L, "run_ensure",
                               lambda *a, **k: (True, "rc=0 健康")):
            self.assertEqual(L.cmd_watchdog(self.root, p), 0)
        self.assertTrue(os.path.isfile(
            os.path.join(self.logs, "autoloop-watchdog.heartbeat")))
        path = os.path.join(self.logs, "autoloop-watchdog-events.jsonl")
        with open(path, encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(recs), 1)
        self.assertEqual((recs[0]["task"], recs[0]["outcome"]), ("-", "ok"))
        self.assertIn("rc=0", recs[0]["reason"])

    def test_watchdog_once_error_rc1(self):
        p = L.parse_args(["watchdog", "--once"])
        with mock.patch.object(L, "run_ensure",
                               lambda *a, **k: (False, "rc=1 拉起失败")):
            self.assertEqual(L.cmd_watchdog(self.root, p), 1)
        self.assertEqual(_outcomes(self.logs, "watchdog"), ["error"])

    def test_watchdog_single_instance_rejected(self):
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        wd_lock = os.path.join(lock_dir, L.WATCHDOG_LOCK)
        fd = L.lock._open_lock(wd_lock)
        self.assertTrue(L.lock._acquire(fd))
        try:
            self.assertEqual(L.cmd_watchdog(self.root, L.parse_args(["watchdog"])), 1)
            self.assertFalse(os.path.isfile(os.path.join(lock_dir, L.WATCHDOG_PID)),
                             "拒启时不应写 PID 文件")
        finally:
            L.lock._release(fd)
            os.close(fd)

    def test_stop_kills_watchdog(self):
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        fake = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        pidfile = os.path.join(lock_dir, L.WATCHDOG_PID)
        with open(pidfile, "w", encoding="ascii") as f:
            f.write(str(fake.pid))
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(L.cmd_stop(self.root), 0)
        self.assertFalse(os.path.isfile(pidfile))
        fake.wait(timeout=10)
        self.assertLess(fake.returncode, 0)

    def test_status_shows_watchdog_lines(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            L.cmd_status(self.root, L.parse_args(["status"]))
        out = buf.getvalue()
        self.assertIn("看门狗: 已停止", out)
        self.assertIn("watchdog", out)


class LauncherE2ETests(_RootCase):
    """子进程 e2e：stub 角色核心 + 临时项目根，绝不触碰真实任务/真实 LLM。"""

    def setUp(self):
        super().setUp()
        self.stub_lib = os.path.join(self.root, "stublib")
        os.makedirs(self.stub_lib)
        for role in ("coder", "reviewer"):
            with open(os.path.join(self.stub_lib, "autoloop_%s.py" % role), "w",
                      encoding="utf-8") as f:
                f.write(STUB_CORE % {"role": role})
        self.env = dict(os.environ)
        self.env["AUTOLOOP_LIB_DIR"] = self.stub_lib
        self.env["AIOS_PROJECT_ROOT"] = self.root
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.calls_file = os.path.join(self.root, "stub_calls.txt")

    def _run(self, *args, **kw):
        return subprocess.run(
            [PY, LAUNCHER] + list(args), cwd=self.root, env=self.env,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=kw.pop("timeout", 90), **kw)

    def _calls(self):
        if not os.path.isfile(self.calls_file):
            return []
        with open(self.calls_file, encoding="utf-8") as f:
            return [l.rstrip("\n") for l in f if l.strip()]

    def _wait_for(self, predicate, timeout, step=0.25):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(step)
        return predicate()

    def test_coder_once_arg_passthrough(self):
        proc = self._run("coder", "--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._calls(), ["coder --once --llm pi"])

    def test_both_once_order(self):
        proc = self._run("both", "--once")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._calls(), ["coder --once --llm pi",
                                         "reviewer --once --llm pi"])

    def test_invalid_args_rejected(self):
        self.assertEqual(self._run().returncode, 1)
        self.assertEqual(self._run("wat").returncode, 1)
        self.assertEqual(self._run("coder", "--foreground").returncode, 1)

    def test_both_background_and_stop(self):
        proc = self._run("both", "--interval", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lock_dir = os.path.join(self.root, "runtime", "locks")
        pidfile = os.path.join(lock_dir, L.BOTH_PID)
        try:
            self.assertTrue(self._wait_for(lambda: os.path.isfile(pidfile), 15),
                            "后台启动后应出现 PID 文件")
            with open(pidfile, encoding="ascii") as f:
                pid = int(f.read().strip())
            self.assertTrue(L._pid_alive(pid), "PID 应存活")
            self.assertTrue(self._wait_for(
                lambda: len(self._calls()) >= 4, 25),
                "常驻应交替运行 coder/reviewer ≥2 轮，实际: %r" % (self._calls(),))
            calls = self._calls()
            self.assertEqual(calls[0].split()[0], "coder")
            self.assertEqual(calls[1].split()[0], "reviewer")
            stop = self._run("stop")
            self.assertEqual(stop.returncode, 0, stop.stderr)
            self.assertTrue(self._wait_for(
                lambda: not os.path.isfile(pidfile)
                and not L._pid_alive(pid), 15),
                "stop 后进程应退出且 PID 文件清理")
        finally:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    L.cmd_stop(self.root)  # 兜底清理，避免残留常驻进程

    def _heartbeat_files(self):
        return [os.path.join(self.logs_dir, "autoloop-%s.heartbeat" % n)
                for n in ("coder", "reviewer")]

    def test_ensure_spawns_when_dead_and_idempotent(self):
        """验收：杀掉 both 后运行一次能拉起；连续两次不双实例（no-op）。"""
        proc = self._run("ensure")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pidfile = os.path.join(self.root, "runtime", "locks", L.BOTH_PID)
        self.assertTrue(self._wait_for(lambda: os.path.isfile(pidfile), 15),
                        "ensure 后应出现 PID 文件")
        with open(pidfile, encoding="ascii") as f:
            pid = int(f.read().strip())
        self.assertTrue(L._pid_alive(pid))
        try:
            second = self._run("ensure")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("no-op", second.stdout, "健康时第二次 ensure 应 no-op")
            with open(pidfile, encoding="ascii") as f:
                self.assertEqual(int(f.read().strip()), pid, "不应产生第二实例")
        finally:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    L.cmd_stop(self.root)

    def test_ensure_noop_when_pid_alive(self):
        """壳存活但无心跳（进程太新/无 stub 心跳）→ 只信 PID，no-op 不误杀。"""
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        fake = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        pidfile = os.path.join(lock_dir, L.BOTH_PID)
        with open(pidfile, "w", encoding="ascii") as f:
            f.write(str(fake.pid))
        try:
            proc = self._run("ensure")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("no-op", proc.stdout)
            self.assertTrue(L._pid_alive(fake.pid), "健康判定不得杀壳")
        finally:
            fake.terminate()
            fake.wait(timeout=10)
            os.remove(pidfile)

    def test_ensure_restarts_when_heartbeat_stale(self):
        """壳存活但心跳停滞 → restart：先杀僵死实例再拉起新实例。"""
        lock_dir = os.path.join(self.root, "runtime", "locks")
        os.makedirs(lock_dir, exist_ok=True)
        self.logs_dir = os.path.join(self.root, "runtime", "logs")
        os.makedirs(self.logs_dir, exist_ok=True)
        fake = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        pidfile = os.path.join(lock_dir, L.BOTH_PID)
        with open(pidfile, "w", encoding="ascii") as f:
            f.write(str(fake.pid))
        old = time.time() - 99999
        for hb in self._heartbeat_files():
            with open(hb, "w", encoding="utf-8") as f:
                f.write("stale")
            os.utime(hb, (old, old))
        try:
            proc = self._run("ensure")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # poll() 先收尸（直接子进程死后成僵尸，_pid_alive 对僵尸返回 True，
            # 真实场景 daemon 为孤儿由 init 收尸，无此问题）
            self.assertTrue(self._wait_for(lambda: fake.poll() is not None, 15),
                            "僵死实例应被终止")
            self.assertFalse(L._pid_alive(fake.pid))
            self.assertTrue(self._wait_for(lambda: os.path.isfile(pidfile), 15),
                            "restart 后应拉起新实例（新 PID 文件）")
            with open(pidfile, encoding="ascii") as f:
                new_pid = int(f.read().strip())
            self.assertNotEqual(new_pid, fake.pid)
            self.assertTrue(L._pid_alive(new_pid))
        finally:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    L.cmd_stop(self.root)
                    try:
                        fake.terminate()
                        fake.wait(timeout=5)
                    except Exception:  # noqa: BLE001 — 已被 restart 击杀则忽略
                        pass

    def test_watchdog_once_e2e(self):
        """验收：--once 单轮真实链路（ensure 子进程→拉起 stub both）+ 心跳/事件落盘。"""
        try:
            proc = self._run("watchdog", "--once")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            hb = os.path.join(self.logs, "autoloop-watchdog.heartbeat")
            ev = os.path.join(self.logs, "autoloop-watchdog-events.jsonl")
            self.assertTrue(os.path.isfile(hb), "巡检应写看门狗心跳")
            self.assertTrue(os.path.isfile(ev), "巡检应写看门狗事件")
            self.assertEqual(_outcomes(self.logs, "watchdog"), ["ok"])
        finally:
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    L.cmd_stop(self.root)  # 兑底清理 ensure 拉起的 both

    def test_watchdog_resident_lifecycle(self):
        """验收：常驻看门狗→心跳/事件→ensure 拉起 both→stop 停止且 PID 清理（TASK-111）。"""
        proc = subprocess.Popen(
            [PY, LAUNCHER, "watchdog", "--interval", "1"],
            cwd=self.root, env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
        wd_pidfile = os.path.join(self.root, "runtime", "locks", L.WATCHDOG_PID)
        hb = os.path.join(self.logs, "autoloop-watchdog.heartbeat")
        ev = os.path.join(self.logs, "autoloop-watchdog-events.jsonl")
        both_pidfile = os.path.join(self.root, "runtime", "locks", L.BOTH_PID)
        try:
            self.assertTrue(self._wait_for(lambda: os.path.isfile(wd_pidfile), 15),
                            "看门狗启动后应出现 PID 文件")
            self.assertTrue(self._wait_for(
                lambda: os.path.isfile(hb) and os.path.isfile(ev), 15),
                "巡检应写心跳与事件（ensure 子进程被真实调用）")
            self.assertTrue(self._wait_for(
                lambda: os.path.isfile(both_pidfile), 30),
                "看门狗应经 ensure 拉起 both 壳（判定链路真实走通）")
            stop = self._run("stop")
            self.assertEqual(stop.returncode, 0, stop.stderr)

            # settle 收敛验收：stop 后容忍残余 fork 竞态（ensure 被杀前已 fork、
            # both 子进程延迟写 PID）——PID 复现则再 stop（≤3 次），连续 3 拍全净
            # （间隔 0.5s）才算收编完成；最终未收敛时附诊断信息。
            def _clear():
                return (not os.path.isfile(wd_pidfile)
                        and not os.path.isfile(both_pidfile))

            settled, stops, streak = False, 1, 0
            deadline = time.time() + 45
            while time.time() < deadline:
                if _clear():
                    streak += 1
                    if streak >= 3:
                        settled = True
                        break
                else:
                    streak = 0
                    if stops < 3:
                        self._run("stop")  # 复活实例 → 再收
                        stops += 1
                time.sleep(0.5)
            self.assertTrue(
                settled,
                "stop 后 45s 未收敛（stop×%d）；wd_pid=%s both_pid=%s；看门狗输出尾: %r"
                % (stops, os.path.isfile(wd_pidfile), os.path.isfile(both_pidfile),
                   (proc.stdout.read() or "")[-200:] if proc.stdout else None))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            with contextlib.redirect_stderr(io.StringIO()):
                with contextlib.redirect_stdout(io.StringIO()):
                    L.cmd_stop(self.root)  # 兑底清理，避免残留常驻进程


if __name__ == "__main__":
    unittest.main()
