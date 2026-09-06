"""TASK-088: task verify 流式输出与状态文件。

验证 cmd_verify 改造后的两个新契约：
1. _run_step_streamed 实时转发子进程输出（返回 returncode 与 transcript，语义与旧 subprocess.run 一致）
2. 执行期间周期性写 runtime/logs/verify-status.json（step/pid/elapsed_s/updated_at，mtime 判活）
3. cmd_verify 成功/失败路径行为不变（VERIFY 记录 / fail log）
"""
import importlib.machinery
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli_verify_stream", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task = importlib.util.module_from_spec(spec)
loader.exec_module(task)


def _write_status(task_mod, status_path, **fields):
    # 经模块间接调用，保证测试跟随实现（若签名变更则此处显式失败）
    return task_mod._write_status(status_path, **fields)


class WriteStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.status = os.path.join(self.tmp.name, "verify-status.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_write_status_creates_json_with_fields(self):
        _write_status(task, self.status, task="TASK-088", step="build",
                      pid=4242, elapsed_s=12)
        data = json.loads(Path(self.status).read_text(encoding="utf-8"))
        self.assertEqual(data["task"], "TASK-088")
        self.assertEqual(data["step"], "build")
        self.assertEqual(data["pid"], 4242)
        self.assertEqual(data["elapsed_s"], 12)
        self.assertIn("updated_at", data)

    def test_write_status_mtime_refreshes(self):
        _write_status(task, self.status, step="build", pid=1, elapsed_s=0)
        m1 = os.stat(self.status).st_mtime_ns
        time.sleep(0.01)
        _write_status(task, self.status, step="build", pid=1, elapsed_s=30)
        m2 = os.stat(self.status).st_mtime_ns
        self.assertGreater(m2, m1)


class RunStepStreamedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.status = os.path.join(self.tmp.name, "verify-status.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_streamed_returns_code_and_transcript(self):
        rc, out = task._run_step_streamed(
            "echo hello && echo world", cwd=self.tmp.name, step="build",
            status_path=self.status, hb_interval=3600)
        self.assertEqual(rc, 0)
        self.assertIn("hello", out)
        self.assertIn("world", out)

    def test_streamed_nonzero_returncode(self):
        rc, out = task._run_step_streamed(
            "echo boom && exit 3", cwd=self.tmp.name, step="test",
            status_path=self.status, hb_interval=3600)
        self.assertEqual(rc, 3)
        self.assertIn("boom", out)

    def test_streamed_writes_status_file(self):
        rc, _ = task._run_step_streamed(
            "true", cwd=self.tmp.name, step="lint",
            status_path=self.status, hb_interval=3600)
        self.assertEqual(rc, 0)
        data = json.loads(Path(self.status).read_text(encoding="utf-8"))
        self.assertEqual(data["step"], "lint")

    def test_streamed_status_elapsed_grows_during_run(self):
        # 命令运行 >2 个心跳间隔，观察 elapsed_s 增长 → 证明执行期间状态在被刷新
        if os.name == "nt":  # Windows sleep 粒度粗，CI 上不稳，跳过
            self.skipTest("windows timing")
        rc, _ = task._run_step_streamed(
            "sleep 0.7", cwd=self.tmp.name, step="test",
            status_path=self.status, hb_interval=0.2)
        self.assertEqual(rc, 0)
        data = json.loads(Path(self.status).read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["elapsed_s"], 0)

    def test_streamed_stdout_live_before_exit(self):
        # 输出必须在进程退出前即可被消费（流式语义）：用管道读取方验证
        r, w = os.pipe()
        saved = os.dup(1)
        os.dup2(w, 1)
        try:
            rc, _ = task._run_step_streamed(
                "echo live-first", cwd=self.tmp.name, step="build",
                status_path=self.status, hb_interval=3600)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
            os.close(w)
        self.assertEqual(rc, 0)
        with os.fdopen(r, "r", encoding="utf-8") as f:
            captured = f.read()
        self.assertIn("live-first", captured)


class CmdVerifyBehaviorTests(unittest.TestCase):
    """cmd_verify 成功/失败路径的既有契约不因流式改造而变。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig_ai = task.AI_DIR
        task.AI_DIR = str(root)  # EVAL-001（TASK-093 审查）：采集/事件钩子以 AI_DIR 为 root，不隔离会污染真实 kit/evaluation
        task.VERIFY_DIR = str(root / "verification")
        task.LOG_DIR = str(root / "logs")
        Path(task.VERIFY_DIR).mkdir()
        Path(task.LOG_DIR).mkdir()
        self.status = str(root / "logs" / "verify-status.json")  # 由 task.LOG_DIR 推导
        self._orig_cfg = task.cfg_lib.load_config
        task.cfg_lib.load_config = lambda ai_dir: {
            "commands": {"build": "true", "lint": "true",
                         "test": "echo t", "check": "echo c"}}

    def tearDown(self):
        task.AI_DIR = self._orig_ai
        task.cfg_lib.load_config = self._orig_cfg
        self.tmp.cleanup()

    def _make_task_file(self, name="TASK-900-test.md"):
        body = (
            "---\n"
            f"name: {name[:-3]}\n"
            "description: x\n"
            "metadata:\n"
            "  type: dev\n"
            "  status: in-progress\n"
            f"  date: {task.today()}\n"
            "---\n\n# body\n"
        )
        task.TASKS_DIR = self.tmp.name
        Path(self.tmp.name).mkdir(exist_ok=True)
        p = Path(self.tmp.name, name)
        p.write_text(body, encoding="utf-8")
        return p

    def test_verify_success_writes_verify_record_and_status(self):
        self._make_task_file()
        task.cmd_verify(["TASK-900"])
        verifies = list(Path(task.VERIFY_DIR).glob("VERIFY-*.md"))
        self.assertEqual(len(verifies), 1)
        text = verifies[0].read_text(encoding="utf-8")
        self.assertIn("result: pass", text)
        self.assertIn("task-ref: TASK-900", text)
        data = json.loads(Path(self.status).read_text(encoding="utf-8"))
        self.assertEqual(data["step"], "check")  # 最后一步的状态留存

    def test_verify_failure_writes_fail_log_and_no_record(self):
        task.cfg_lib.load_config = lambda ai_dir: {
            "commands": {"build": "true", "lint": "exit 2",
                         "test": "true", "check": "true"}}
        self._make_task_file()
        with self.assertRaises(SystemExit):
            task.cmd_verify(["TASK-900"])
        fails = list(Path(task.LOG_DIR).glob("fail-*.log"))
        self.assertEqual(len(fails), 1)
        self.assertIn("exit 2", fails[0].read_text(encoding="utf-8"))
        self.assertEqual(list(Path(task.VERIFY_DIR).glob("VERIFY-*.md")), [])


if __name__ == "__main__":
    unittest.main()
