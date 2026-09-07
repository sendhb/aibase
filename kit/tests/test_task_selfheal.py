"""TASK-107 — task CLI 自愈面单测：cmd_review 守卫/唤醒/索引刷新 + cmd_stale 滞留检测。

覆盖：
- cmd_review：转 in-review 后 INDEX.md 与 frontmatter 一致（治索引漂移，对齐 cmd_approve）
- _reviewer_alive_guard：心跳缺失/停滞 → 输出「reviewer 不在岗」告警；新鲜 → 静默
- _wake_reviewer：心跳停滞 → spawn kit/cli/lib/autoloop_reviewer.py --once（stub 核心
  落 marker 验证）；心跳正常 → 不 spawn；分离语义与内层锁防重入由 autoloop 侧保证
- cmd_stale：in-review 滞留（metadata.updated 早于今日）→ exit 2 + 逐条明细；
  健康 exit 0；非 in-review 忽略；updated 不可解析跳过；--hours 阈值可调
- _stale_hours 纯函数锚点（固定 now，不依赖墙钟）

**安全边界**：不调真实 LLM、不碰真实 runtime/——全部在临时根内（覆写 task 模块
目录变量）；唤醒测试用临时根内的 stub reviewer 脚本；cmd_review 内 _wake_reviewer
以 recorder 替身拦截（不真 spawn）。
"""
import contextlib
import datetime
import importlib.machinery
import importlib.util
import io
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli_selfheal", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task = importlib.util.module_from_spec(spec)
loader.exec_module(task)


class _TaskRootCase(unittest.TestCase):
    """临时项目根：覆写 task 模块目录变量（与 test_task_review_guard 同章法）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("TASKS_DIR", "STATE_DIR", "LOG_DIR", "VERIFY_DIR", "REVIEW_DIR"):
            val = str(self.root / name.lower().replace("_dir", ""))
            setattr(task, name, val)
            Path(val).mkdir(parents=True, exist_ok=True)
        task.AI_DIR = str(self.root)

    def write_task(self, name="TASK-910-t", status="in-progress", risk="P1",
                   priority="P1", reviewer="rev-1", updated=None):
        body = f"""---
name: {name}
description: t
metadata:
  type: task
  status: {status}
  created: 2026-01-01
  updated: {updated or task.today()}
  priority: {priority}
  risk: {risk}
  approval-ref: none
  assignee: agent-x
  reviewer: {reviewer}
  rework-count: 0
  depends-on: []
---

## 验收标准
- [ ] ok
"""
        path = Path(task.TASKS_DIR, name + ".md")
        path.write_text(body, encoding="utf-8")
        return str(path)

    def set_heartbeat(self, age_s=None):
        """写/清 reviewer 心跳文件；age_s None = 不写（从未运行）。"""
        hb = Path(task.LOG_DIR, "autoloop-reviewer.heartbeat")
        if age_s is None:
            hb.unlink(missing_ok=True)
        else:
            hb.write_text("hb", encoding="utf-8")
            old = time.time() - age_s
            os.utime(hb, (old, old))

    def run_cmd(self, fn, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn(*args)
        return out.getvalue()

    def days_ago_iso(self, n):
        return (datetime.date.today() - datetime.timedelta(days=n)).isoformat()


class ReviewIndexAndGuardTests(_TaskRootCase):
    def _review(self, task_id, wake=False):
        args = [task_id] + (["--wake"] if wake else [])
        out = io.StringIO()
        wakes = []
        with mock.patch.object(task, "_wake_reviewer",
                               lambda root, log_dir: wakes.append(root) or 0):
            with contextlib.redirect_stdout(out):
                task.cmd_review(args)
        return out.getvalue(), wakes

    def test_review_refreshes_index_to_in_review(self):
        self.write_task(status="in-progress")
        self.set_heartbeat(0)  # 心跳新鲜 → 守卫静默
        out, wakes = self._review("TASK-910-t")
        self.assertEqual(wakes, [])
        idx = Path(task.TASKS_DIR, "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("| TASK-910-t | in-review |", idx)
        _, fm = task.load_task(str(Path(task.TASKS_DIR, "TASK-910-t.md")))
        self.assertEqual(fm.get("metadata.status"), "in-review")

    def test_review_warns_when_reviewer_dead(self):
        self.write_task(status="in-progress")
        self.set_heartbeat(99999)  # 心跳停滞
        out, _ = self._review("TASK-910-t")
        self.assertIn("reviewer 不在岗", out)
        self.assertIn("autoloop ensure", out)

    def test_review_warns_when_never_ran(self):
        self.write_task(status="in-progress")
        self.set_heartbeat(None)  # 从未运行
        out, _ = self._review("TASK-910-t")
        self.assertIn("reviewer 不在岗", out)

    def test_review_quiet_when_heartbeat_fresh(self):
        self.write_task(status="in-progress")
        self.set_heartbeat(5)
        out, _ = self._review("TASK-910-t")
        self.assertNotIn("reviewer 不在岗", out)

    def test_review_wake_flag_calls_wake(self):
        self.write_task(status="in-progress")
        self.set_heartbeat(99999)
        out, wakes = self._review("TASK-910-t", wake=True)
        self.assertEqual(wakes, [str(self.root)])
        idx = Path(task.TASKS_DIR, "INDEX.md").read_text(encoding="utf-8")
        self.assertIn("in-review", idx)


class WakeReviewerTests(_TaskRootCase):
    STUB_REVIEWER = (
        "import os\n"
        "marker = os.path.join(os.path.dirname(os.path.abspath(__file__)),\n"
        "                      'wake_marker.txt')\n"
        "open(marker, 'w').write('woke')\n"
    )

    def _install_stub(self):
        lib = Path(self.root, "kit", "cli", "lib")
        lib.mkdir(parents=True, exist_ok=True)
        script = lib / "autoloop_reviewer.py"
        script.write_text(self.STUB_REVIEWER, encoding="utf-8")
        return str(script)

    def _marker(self):
        return Path(self.root, "kit", "cli", "lib", "wake_marker.txt")

    def test_wake_spawns_once_when_stale(self):
        self._install_stub()
        self.set_heartbeat(99999)
        out = self.run_cmd(task._wake_reviewer, str(self.root), task.LOG_DIR)
        self.assertIn("已后台拉起", out)
        deadline = time.time() + 10
        while time.time() < deadline and not self._marker().exists():
            time.sleep(0.1)
        self.assertTrue(self._marker().exists(), "stub reviewer 应被 spawn 执行")

    def test_wake_skips_when_fresh(self):
        self._install_stub()
        self.set_heartbeat(1)
        out = self.run_cmd(task._wake_reviewer, str(self.root), task.LOG_DIR)
        self.assertIn("无需唤醒", out)
        time.sleep(0.5)
        self.assertFalse(self._marker().exists())

    def test_wake_missing_script_reports(self):
        self.set_heartbeat(99999)
        out = self.run_cmd(task._wake_reviewer, str(self.root), task.LOG_DIR)
        self.assertIn("找不到 reviewer 核心", out)


class StaleTests(_TaskRootCase):
    def test_stale_hours_pure(self):
        base = datetime.datetime(2026, 1, 1)
        now = base.timestamp() + 36 * 3600
        self.assertAlmostEqual(task._stale_hours("2026-01-01", now), 36.0)
        self.assertIsNone(task._stale_hours("not-a-date", now))
        self.assertEqual(task._stale_hours("2099-01-01", now), 0.0)

    def test_stale_exits_2_and_lists(self):
        self.write_task(name="TASK-911-stale", status="in-review",
                        updated=self.days_ago_iso(3))
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                task.cmd_stale([])
        self.assertEqual(ctx.exception.code, 2)
        text = out.getvalue()
        m = re.search(r"滞留 ([\d.]+) 小时", text)
        self.assertIsNotNone(m, text)
        self.assertGreaterEqual(float(m.group(1)), 72.0)  # 日期粒度：≥3 天自 00:00 起算

    def test_stale_healthy_exits_0(self):
        self.write_task(name="TASK-912-fresh", status="in-review",
                        updated=task.today())
        out = self.run_cmd(task.cmd_stale, ["--hours", "100000"])
        self.assertIn("无滞留", out)

    def test_stale_ignores_other_statuses_and_bad_dates(self):
        self.write_task(name="TASK-913-done", status="done",
                        updated=self.days_ago_iso(3))
        self.write_task(name="TASK-914-bad", status="in-review", updated="garbage")
        out = self.run_cmd(task.cmd_stale, [])
        self.assertIn("无滞留", out)

    def test_stale_hours_flag_tightens(self):
        self.write_task(name="TASK-915-old", status="in-review",
                        updated=self.days_ago_iso(3))
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(out):
                task.cmd_stale(["--hours", "1"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("TASK-915-old 滞留", out.getvalue())


if __name__ == "__main__":
    unittest.main()
