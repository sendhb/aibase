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

PY = sys.executable
LAUNCHER = os.path.join(_REPO, "kit", "cli", "autoloop")

STUB_CORE = """import os, sys
with open(os.path.join(os.getcwd(), "stub_calls.txt"), "a", encoding="utf-8") as f:
    f.write("%(role)s " + " ".join(sys.argv[1:]) + "\\n")
"""


def _write_task(tasks_dir, name, status="open", risk="P2", priority="P2",
                reviewer="any", approval="none", rework="0", assignee="any"):
    os.makedirs(tasks_dir, exist_ok=True)
    path = os.path.join(tasks_dir, name + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(
            "---\nname: %s\ndescription: t\nmetadata:\n  type: task\n"
            "  status: %s\n  priority: %s\n  risk: %s\n  approval-ref: %s\n"
            "  assignee: %s\n  reviewer: %s\n  rework-count: %s\n"
            "  depends-on: []\n---\n\n- [x] ok\n" % (
                name, status, priority, risk, approval, assignee, reviewer, rework))
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


if __name__ == "__main__":
    unittest.main()
