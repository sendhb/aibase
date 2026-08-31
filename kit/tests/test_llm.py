"""TASK-027 — llm.py + events.py 单测（TASK-022 交付）。

覆盖：
- _provider_argv：pi / claude ± unattended / deepseek / 大小写归一 / 未知 ValueError
- run_llm 退出码契约：0 成功 / 非零透传 / 124 超时杀进程 / 127 可执行缺失
  （全程 mock argv，**零真实 provider 调用**——pi/claude 是本机真实命令）
- _deepseek_env：env 优先 / models.json 注入 / 缺失静默降级（mock expanduser，
  绝不读真实 ~/.pi/agent/models.json）
- run_llm_with_events：ok / error / timeout 三事件落 autoloop-<name>-events.jsonl；
  用量记录 _record_usage（TASK-030 接线：outcome 映射 / log 增量 / 静默失败）
- events.py：heartbeat 写 epoch / emit_event 枚举与 JSON 行 / validate_events
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_HERE), "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import events  # noqa: E402
import llm  # noqa: E402

PY = sys.executable


class RecordUsageTests(unittest.TestCase):
    """TASK-030 — _record_usage 直接单测（append 接线 + 静默失败）。"""

    def test_append_called_with_fields(self):
        with mock.patch("token_usage.append") as ap:
            llm._record_usage("reviewer", "TASK-902", "claude",
                              "timeout", 2.5, 40)
        ap.assert_called_once_with(role="reviewer", task="TASK-902",
                                   provider="claude", outcome="timeout",
                                   duration_s=2.5, output_bytes=40)

    def test_swallow_exceptions(self):
        with mock.patch("token_usage.append", side_effect=OSError("disk full")):
            llm._record_usage("coder", "TASK-901", "pi", "ok", 1.0, 10)  # 不抛


class ProviderArgvTests(unittest.TestCase):
    def test_pi(self):
        self.assertEqual(llm._provider_argv("pi", "P"),
                         ["pi", "-p", "P", "--no-session"])

    def test_claude_unattended_flag(self):
        self.assertEqual(llm._provider_argv("claude", "P", unattended=True),
                         ["claude", "-p", "P", "--dangerously-skip-permissions"])
        self.assertEqual(llm._provider_argv("claude", "P"), ["claude", "-p", "P"])

    def test_deepseek(self):
        self.assertEqual(llm._provider_argv("deepseek", "P"),
                         ["codewhale", "exec", "--auto", "P"])

    def test_case_normalized(self):
        self.assertEqual(llm._provider_argv("  PI ", "P"),
                         ["pi", "-p", "P", "--no-session"])

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            llm._provider_argv("gpt5", "P")
        with self.assertRaises(ValueError):
            llm._provider_argv(None, "P")


class RunLlmExitCodeTests(unittest.TestCase):
    """退出码契约：0/非零/124/127。argv 全部 mock，不真调任何 provider。"""

    def _run(self, argv, **kw):
        with mock.patch.object(llm, "_provider_argv", return_value=argv):
            return llm.run_llm("pi", "P", **kw)

    def test_success_zero(self):
        self.assertEqual(self._run([PY, "-c", ""]), 0)

    def test_exit_code_passthrough(self):
        self.assertEqual(self._run([PY, "-c", "import sys; sys.exit(3)"]), 3)

    def test_missing_executable_127(self):
        self.assertEqual(self._run(["zzz-no-such-binary-9x7"]), 127)

    def test_timeout_124_and_killed(self):
        rc = self._run([PY, "-c", "import time; time.sleep(30)"], timeout=1)
        self.assertEqual(rc, 124)

    def test_bad_provider_valueerror_not_subprocess(self):
        with self.assertRaises(ValueError):
            llm.run_llm("no-such-provider", "P")


class DeepseekEnvTests(unittest.TestCase):
    def test_env_var_priority(self):
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
            self.assertEqual(llm._deepseek_env()["DEEPSEEK_API_KEY"], "env-key")

    def test_models_json_injection(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        models_dir = os.path.join(tmp, ".pi", "agent")
        os.makedirs(models_dir)
        with open(os.path.join(models_dir, "models.json"), "w", encoding="utf-8") as f:
            json.dump({"providers": {"deepseek": {"apiKey": "file-key"}}}, f)
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("os.path.expanduser", return_value=tmp):
                self.assertEqual(llm._deepseek_env()["DEEPSEEK_API_KEY"], "file-key")

    def test_missing_models_silent_degrade(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("os.path.expanduser", return_value=tmp):
                self.assertNotIn("DEEPSEEK_API_KEY", llm._deepseek_env())

    def test_corrupt_models_silent_degrade(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        models_dir = os.path.join(tmp, ".pi", "agent")
        os.makedirs(models_dir)
        with open(os.path.join(models_dir, "models.json"), "w", encoding="utf-8") as f:
            f.write("{broken json")
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("os.path.expanduser", return_value=tmp):
                self.assertNotIn("DEEPSEEK_API_KEY", llm._deepseek_env())


class RunLlmWithEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = os.path.join(self.tmp.name, "logs")
        # TASK-030：用量记录会写到真实项目根 runtime/logs，测试必须阻断副作用
        p = mock.patch.object(llm, "_record_usage")
        self.record_usage = p.start()
        self.addCleanup(p.stop)

    def _run(self, argv, timeout=None, log_file=None):
        with mock.patch.object(llm, "_provider_argv", return_value=argv):
            return llm.run_llm_with_events("pi", "P", log_dir=self.log_dir,
                                           name="coder", task="TASK-901",
                                           timeout=timeout, log_file=log_file)

    def _outcomes(self, name="coder"):
        path = events.events_path(self.log_dir, name)
        with open(path, encoding="utf-8") as f:
            return [json.loads(l)["outcome"] for l in f if l.strip()]

    def test_ok_event(self):
        self.assertEqual(self._run([PY, "-c", ""]), 0)
        self.assertEqual(self._outcomes(), ["ok"])

    def test_error_event(self):
        self.assertEqual(self._run([PY, "-c", "import sys; sys.exit(3)"]), 3)
        self.assertEqual(self._outcomes(), ["error"])

    def test_usage_outcome_ok_with_log_delta(self):
        log_file = os.path.join(self.tmp.name, "llm.log")
        self.assertEqual(self._run([PY, "-c", "print('x' * 100)"],
                                   log_file=log_file), 0)
        self.assertEqual(self.record_usage.call_count, 1)
        a = self.record_usage.call_args.args
        self.assertEqual(a[0], "coder")     # role = name
        self.assertEqual(a[1], "TASK-901")  # task
        self.assertEqual(a[2], "pi")        # provider
        self.assertEqual(a[3], "ok")        # outcome
        self.assertGreater(a[4], 0)         # duration_s > 0
        self.assertGreater(a[5], 0)         # output_bytes = 文件增量 > 0

    def test_usage_terminal_zero_bytes(self):
        self.assertEqual(self._run([PY, "-c", ""]), 0)
        self.assertEqual(self.record_usage.call_args.args[5], 0)  # 无 log → 0

    def test_usage_outcome_error(self):
        self.assertEqual(self._run([PY, "-c", "import sys; sys.exit(3)"]), 3)
        self.assertEqual(self.record_usage.call_args.args[3], "error")

    def test_timeout_event(self):
        self.assertEqual(self._run([PY, "-c", "import time; time.sleep(30)"],
                                   timeout=1), 124)
        self.assertEqual(self._outcomes(), ["timeout"])

    def test_log_file_appended(self):
        log_file = os.path.join(self.tmp.name, "out.log")
        with mock.patch.object(llm, "_provider_argv",
                               return_value=[PY, "-c", "print('hi')"]):
            llm.run_llm_with_events("pi", "P", log_dir=self.log_dir,
                                    name="coder", task="TASK-901",
                                    log_file=log_file)
        with open(log_file, encoding="utf-8") as f:
            self.assertIn("hi", f.read())


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = os.path.join(self.tmp.name, "logs")

    def test_heartbeat_writes_epoch(self):
        path = events.heartbeat(self.log_dir, "coder")
        self.assertEqual(os.path.basename(path), "autoloop-coder.heartbeat")
        with open(path, encoding="ascii") as f:
            int(f.read().strip())  # 内容是 epoch 整数

    def test_paths(self):
        self.assertTrue(events.heartbeat_path("/x", "r").endswith("autoloop-r.heartbeat"))
        self.assertTrue(events.events_path("/x", "r").endswith("autoloop-r-events.jsonl"))


class EmitEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_dir = os.path.join(self.tmp.name, "logs")

    def test_emit_and_validate(self):
        for outcome in ("ok", "error", "timeout"):
            events.emit_event(self.log_dir, "coder", "TASK-901", outcome)
        errors, total = events.validate_events(self.log_dir, "coder")
        self.assertEqual((errors, total), (0, 3))

    def test_emit_fields(self):
        events.emit_event(self.log_dir, "reviewer", "-", "no_task")
        with open(events.events_path(self.log_dir, "reviewer"), encoding="utf-8") as f:
            data = json.loads(f.readline())
        self.assertEqual(data["task"], "-")
        self.assertEqual(data["outcome"], "no_task")
        self.assertIsInstance(data["ts"], float)

    def test_invalid_outcome_raises(self):
        with self.assertRaises(ValueError):
            events.emit_event(self.log_dir, "coder", "TASK-901", "wow")

    def test_validate_detects_bad_json_and_outcome(self):
        events.emit_event(self.log_dir, "coder", "TASK-901", "ok")
        with open(events.events_path(self.log_dir, "coder"), "a", encoding="utf-8") as f:
            f.write("garbage\n")
            f.write(json.dumps({"ts": 1.0, "task": "T", "outcome": "bogus"}) + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors, total = events.validate_events(self.log_dir, "coder")
        self.assertEqual(total, 3)
        self.assertEqual(errors, 2)

    def test_validate_missing_file(self):
        self.assertEqual(events.validate_events(self.log_dir, "coder"), (0, 0))


if __name__ == "__main__":
    unittest.main()
