"""TASK-100 — per-task LLM 会话日志单测。

覆盖：
- task_log_path 纯函数（路径 = task ID 纯函数，验收断言原文）
- _open_task_log：轮次头写入/round 自增/PID 回填、no_task 回落（不产生 task log）
- run_llm_coder / run_llm_reviewer 会话输出落 per-task log（stub _run_argv，
  真实事件语义不变；多轮共存单文件且时间有序）
- _run_argv on_start 回调（真实 Popen，头部先于子进程输出）
- build_prompt 的 verify tee 指示（pipefail + per-task log 路径）

安全边界：不调用 main()（find_project_root 恒定位真实仓库）；LLM 一律 stub
（mock _resolve_provider_argv / _run_argv 或用 sys.executable 空转），零外部依赖。
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import autoloop_coder as C  # noqa: E402
import autoloop_reviewer as R  # noqa: E402
import events  # noqa: E402
import llm  # noqa: E402

HEADER_RE = re.compile(
    r"^=== \[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\] "
    r"(?P<role>\S+) round (?P<round>\d+) \| provider (?P<provider>\S+) "
    r"\| PID (?P<pid>\d+) ===$")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _task_log(logs, task_id):
    return os.path.join(logs, "tasks", "%s.log" % task_id)


class TaskLogPathTests(unittest.TestCase):
    def test_pure_function_of_task_id(self):
        # 验收标准第 2 条原文断言
        self.assertEqual(C.task_log_path("TASK-091"),
                         "runtime/logs/tasks/TASK-091.log")
        self.assertEqual(C.task_log_path("TASK-100"),
                         "runtime/logs/tasks/TASK-100.log")

    def test_distinct_ids_distinct_paths(self):
        self.assertNotEqual(C.task_log_path("TASK-1"), C.task_log_path("TASK-2"))


class _LogsCase(unittest.TestCase):
    """临时 runtime/logs 根（先建目录，与真实入口 main() 的 makedirs 对齐）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = os.path.join(self.tmp.name, "runtime", "logs")
        os.makedirs(self.logs)
        self.daily = os.path.join(self.logs, "autoloop-coder-2026-09-04.log")

    def tearDown(self):
        self.tmp.cleanup()


class OpenTaskLogTests(_LogsCase):
    def test_header_round1_with_pid(self):
        out, on_start = C._open_task_log(self.logs, "TASK-100", role="coder",
                                         provider="pi", fallback_log=None)
        self.addCleanup(out.close)
        on_start(4242)
        heads = [l for l in _read(_task_log(self.logs, "TASK-100")).splitlines()
                 if l.startswith("=== [")]
        self.assertEqual(len(heads), 1)
        m = HEADER_RE.match(heads[0])
        self.assertIsNotNone(m, heads[0])
        self.assertEqual((m.group("role"), m.group("round"),
                          m.group("provider"), m.group("pid")),
                         ("coder", "1", "pi", "4242"))

    def test_round_increments_across_handles(self):
        for i in (1, 2):
            out, on_start = C._open_task_log(self.logs, "TASK-100", role="coder",
                                             provider="pi", fallback_log=None)
            on_start(100 + i)
            out.close()
        rounds = [HEADER_RE.match(l).group("round")
                  for l in _read(_task_log(self.logs, "TASK-100")).splitlines()
                  if l.startswith("=== [")]
        self.assertEqual(rounds, ["1", "2"])

    def test_no_task_falls_back_and_creates_no_task_dir(self):
        daily_out, on_start = C._open_task_log(self.logs, "-", role="coder",
                                               provider="pi",
                                               fallback_log=self.daily)
        self.addCleanup(daily_out.close)
        self.assertIsNone(on_start)
        daily_out.write("loop-line\n")
        daily_out.close()
        self.assertIn("loop-line", _read(self.daily))
        self.assertFalse(os.path.exists(os.path.join(self.logs, "tasks")))
        # 无 fallback + 无 task → 不重定向（与旧版 log_file=None 一致）
        self.assertEqual(C._open_task_log(self.logs, None, role="coder",
                                          provider="pi", fallback_log=None),
                         (None, None))

    def test_tasks_dir_created_on_demand(self):
        out, _ = C._open_task_log(self.logs, "TASK-777", role="coder",
                                  provider="pi", fallback_log=None)
        out.close()
        self.assertTrue(os.path.isdir(os.path.join(self.logs, "tasks")))


class RunLlmTaskLogTests(_LogsCase):
    """stub _run_argv：验证 run_llm_* 的落盘路径/头部/事件/daily 分离。"""

    @staticmethod
    def _fake_run_argv(calls, child_text="stub-session-out"):
        def fake(argv, env, timeout=None, stdout=None, stderr=None,
                 root=None, llm_name=None, task_id=None, on_start=None,
                 fatal_scan_path=None, fatal_scan_start=None):
            calls.append((llm_name, task_id))
            if on_start is not None:
                on_start(777)  # 先头部后输出（与真实 Popen 时序一致）
            stdout.write(child_text + "\n")
            return 0
        return fake

    def test_coder_session_lands_in_task_log(self):
        calls, ev = [], []
        with mock.patch.object(C, "_run_argv",
                               self._fake_run_argv(calls)), \
             mock.patch.object(events, "emit_event", lambda *a: ev.append(a)):
            rc = C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901",
                                 log_file=self.daily)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("pi", "TASK-901")])
        body = _read(_task_log(self.logs, "TASK-901"))
        self.assertIn("coder round 1 | provider pi | PID 777 ===", body)
        self.assertIn("stub-session-out", body)
        # 会话输出不再进 daily；ok 事件语义不变
        self.assertFalse(os.path.exists(self.daily))
        self.assertEqual(ev[-1][1:], ("coder", "TASK-901", "ok"))

    def test_reviewer_round_coexists_after_coder(self):
        calls = []
        with mock.patch.object(C, "_run_argv", self._fake_run_argv(calls)):
            C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901")
        with mock.patch.object(R, "_run_argv", self._fake_run_argv(calls)):
            R.run_llm_reviewer("claude", "P", log_dir=self.logs, task="TASK-901")
        heads = [l for l in _read(_task_log(self.logs, "TASK-901")).splitlines()
                 if l.startswith("=== [")]
        self.assertEqual(len(heads), 2)  # 验收第 3 条：两轮 2 头
        self.assertIn("coder round 1 | provider pi", heads[0])
        self.assertIn("reviewer round 2 | provider claude", heads[1])

    def test_no_task_writes_daily_not_task_log(self):
        calls = []
        with mock.patch.object(C, "_run_argv", self._fake_run_argv(calls)):
            C.run_llm_coder("pi", "P", log_dir=self.logs, task="-",
                            log_file=self.daily)
        self.assertFalse(os.path.exists(os.path.join(self.logs, "tasks")))
        self.assertIn("stub-session-out", _read(self.daily))

    def test_popen_failure_writes_no_header(self):
        # 可执行缺失 → 127，无 Popen 即无轮次头，但文件留空（append 语义）
        with mock.patch.object(C, "_resolve_provider_argv",
                               return_value=(["definitely-not-exist-xyz"], None)):
            rc = C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-902")
        self.assertEqual(rc, 127)
        self.assertEqual(_read(_task_log(self.logs, "TASK-902")), "")


class RunArgvOnStartTests(_LogsCase):
    def test_on_start_receives_pid_before_child_output(self):
        pids = []
        path = os.path.join(self.logs, "popen-probe.log")
        with open(path, "a", encoding="utf-8") as fh:
            def on_start(pid):  # 模拟 _open_task_log 的轮次头回调
                pids.append(pid)
                fh.write("=== [PID %d] ===\n" % pid)
                fh.flush()
            rc = C._run_argv([sys.executable, "-c",
                              "import time; time.sleep(0.3); print('child-out')"],
                             None, stdout=fh, stderr=subprocess.STDOUT,
                             on_start=on_start)
        self.assertEqual(rc, 0)
        self.assertEqual(len(pids), 1)
        body = _read(path)
        self.assertLess(body.index("=== ["), body.index("child-out"),
                        "轮次头应先于子进程输出")

    def test_on_start_exception_swallowed(self):
        with open(os.devnull, "w") as fh:
            rc = C._run_argv([sys.executable, "-c", "pass"], None,
                             stdout=fh, stderr=subprocess.STDOUT,
                             on_start=lambda pid: 1 / 0)
        self.assertEqual(rc, 0)  # 回调抛错不影响会话退出码


class ProviderArgvSessionDirTests(unittest.TestCase):
    """TASK-103：_provider_argv session_dir 参数（默认契约不回归）。"""

    def test_pure_function_of_task_id(self):
        # 验收标准第 1 条原文断言
        self.assertEqual(C.task_session_dir("TASK-103"),
                         "runtime/logs/sessions/TASK-103")

    def test_pi_default_contract_unchanged(self):
        # 验收标准第 2 条原文断言：不传 session_dir 与 TASK-022 契约逐字节一致
        self.assertEqual(llm._provider_argv("pi", "P"),
                         ["pi", "-p", "P", "--no-session"])

    def test_pi_with_session_dir(self):
        # 验收标准第 2 条原文断言
        self.assertEqual(llm._provider_argv("pi", "P", session_dir="/s"),
                         ["pi", "-p", "P", "--session-dir", "/s"])

    def test_non_pi_ignores_session_dir(self):
        self.assertEqual(llm._provider_argv("claude", "P", session_dir="/s"),
                         ["claude", "-p", "P"])

    def test_resolve_posix_threads_session_dir(self):
        with mock.patch.object(C.os, "name", "posix"):
            argv, env = C._resolve_provider_argv("pi", "P", session_dir="/s")
        self.assertEqual(argv, ["pi", "-p", "P", "--session-dir", "/s"])

    def test_resolve_windows_branch_session_dir(self):
        # Windows 分支：session_dir 非空 → --session-dir（正斜杠适配 Git Bash）；
        # 为空 → 保持 --no-session（TASK-022 契约）
        with mock.patch.object(C.os, "name", "nt"), \
             mock.patch.object(C, "_find_pi_posix_script",
                               return_value="C:\\npm\\pi"), \
             mock.patch.object(C.shutil, "which", return_value="C:\\bash.exe"):
            argv, _ = C._resolve_provider_argv("pi", "P",
                                               session_dir="C:\\proj\\runtime\\logs\\sessions\\TASK-1")
            self.assertEqual(
                argv,
                ["bash", "C:\\npm\\pi", "-p", "P", "--session-dir",
                 "C:/proj/runtime/logs/sessions/TASK-1"])
            argv, _ = C._resolve_provider_argv("pi", "P", session_dir=None)
            self.assertEqual(
                argv, ["bash", "C:\\npm\\pi", "-p", "P", "--no-session"])


class RunLlmSessionDirTests(_LogsCase):
    """TASK-103：run_llm_* 的 session 目录接线（stub _run_argv 捕获 argv）。"""

    @staticmethod
    def _argv_capturing_run_argv(calls):
        def fake(argv, env, timeout=None, stdout=None, stderr=None,
                 root=None, llm_name=None, task_id=None, on_start=None,
                 fatal_scan_path=None, fatal_scan_start=None):
            calls.append(list(argv))
            stdout.write("stub-session-out\n")
            return 0
        return fake

    def test_coder_pi_creates_and_uses_session_dir(self):
        calls = []
        sdir = os.path.join(self.logs, "sessions", "TASK-901")
        with mock.patch.object(C, "_run_argv",
                               self._argv_capturing_run_argv(calls)):
            rc = C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-901")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(sdir))
        self.assertIn("--session-dir", calls[0])
        self.assertEqual(calls[0][calls[0].index("--session-dir") + 1], sdir)
        self.assertNotIn("--no-session", calls[0])

    def test_no_task_round_keeps_no_session(self):
        calls = []
        with mock.patch.object(C, "_run_argv",
                               self._argv_capturing_run_argv(calls)):
            C.run_llm_coder("pi", "P", log_dir=self.logs, task="-",
                            log_file=self.daily)
        self.assertIn("--no-session", calls[0])
        self.assertNotIn("--session-dir", calls[0])
        self.assertFalse(os.path.exists(os.path.join(self.logs, "sessions")))

    def test_claude_gets_no_session_dir(self):
        calls = []
        with mock.patch.object(C, "_run_argv",
                               self._argv_capturing_run_argv(calls)):
            C.run_llm_coder("claude", "P", log_dir=self.logs, task="TASK-901")
        self.assertNotIn("--session-dir", calls[0])
        self.assertFalse(os.path.exists(os.path.join(self.logs, "sessions")))

    def test_makedirs_failure_degrades_to_no_session(self):
        calls = []
        real_makedirs = os.makedirs

        def boom(path, *a, **kw):
            if "sessions" in str(path):
                raise OSError("disk full")
            return real_makedirs(path, *a, **kw)

        with mock.patch.object(C.os, "makedirs", boom), \
             mock.patch.object(C, "_run_argv",
                               self._argv_capturing_run_argv(calls)):
            rc = C.run_llm_coder("pi", "P", log_dir=self.logs, task="TASK-903")
        self.assertEqual(rc, 0)  # 降级不阻塞会话
        self.assertIn("--no-session", calls[0])
        self.assertNotIn("--session-dir", calls[0])

    def test_reviewer_pi_reuses_session_dir(self):
        calls = []
        sdir = os.path.join(self.logs, "sessions", "TASK-904")
        with mock.patch.object(R, "_run_argv",
                               self._argv_capturing_run_argv(calls)):
            rc = R.run_llm_reviewer("pi", "P", log_dir=self.logs,
                                    task="TASK-904")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isdir(sdir))
        self.assertEqual(calls[0][calls[0].index("--session-dir") + 1], sdir)


class BuildPromptTeeTests(unittest.TestCase):
    def test_verify_streams_to_task_log(self):
        p = C.build_prompt("TASK-100", "TASK-100-per-task-llm-log.md", True, "done")
        self.assertIn(
            "set -o pipefail; python kit/cli/task verify TASK-100 2>&1 "
            "| tee -a runtime/logs/tasks/TASK-100.log", p)

    def test_full_path_prompt_uses_review_step(self):
        p = C.build_prompt("TASK-101", "TASK-101-x.md", False, "review")
        self.assertIn("task verify TASK-101", p)
        self.assertIn("task review TASK-101", p)


if __name__ == "__main__":
    unittest.main()
