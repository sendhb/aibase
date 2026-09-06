"""TASK-101 — cmd_review 前置校验（硬拦截 fast-path 任务）单测。

覆盖：
- reviewer=any/空/none（is_fast_path 判 True）→ task review 非零退出，stderr 含
  "fast-path" 与 "reviewer"，且状态不变、无事件写入（不一致状态无法被创造）
- 指定 reviewer → 正常转 in-review，task-events.jsonl 写入既有 task.review_requested
  事件（事件名与格式不变，TASK-101 只加前置校验）
- P0 任务 reviewer=any → 不拦截（is_fast_path 语义不变：P0/P1 本就需要独立审查，
  reviewer 循环按 status 捡任务不会静默跳过）
"""
import contextlib
import importlib.machinery
import json
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task = importlib.util.module_from_spec(spec)
loader.exec_module(task)


class ReviewGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        task.TASKS_DIR = str(self.root / "tasks")
        task.STATE_DIR = str(self.root / "states")
        task.LOG_DIR = str(self.root / "logs")
        for d in (task.TASKS_DIR, task.STATE_DIR, task.LOG_DIR):
            Path(d).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write_task(self, reviewer="any", status="in-progress", risk="P2", priority="P2"):
        body = f"""---
name: TASK-101-test
metadata:
  type: task
  status: {status}
  created: {task.today()}
  updated: {task.today()}
  priority: {priority}
  risk: {risk}
  approval-ref: none
  assignee: any
  reviewer: {reviewer}
  parent: TASK-000
  depends-on: []
---
# TASK-101

## 验收标准
- [ ] ok
"""
        path = Path(task.TASKS_DIR, "TASK-101-test.md")
        path.write_text(body, encoding="utf-8")
        return str(path)

    def read_events(self):
        path = Path(task.LOG_DIR, "task-events.jsonl")
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # 容错：与生产读取一致，跳过损坏行
        return out

    def read_status(self):
        _, fm = task.load_task(str(Path(task.TASKS_DIR, "TASK-101-test.md")))
        return fm.get("metadata.status")

    def test_review_rejects_unspecified_reviewer(self):
        """reviewer=any（fast-path）→ 非零退出，stderr 含 fast-path 与 reviewer，状态/事件不变。"""
        self.write_task(reviewer="any")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                task.cmd_review(["TASK-101-test"])
        self.assertNotEqual(cm.exception.code, 0)
        err_text = err.getvalue()
        self.assertIn("fast-path", err_text)
        self.assertIn("reviewer", err_text)
        self.assertIn("TASK-101", err_text)  # 文案含任务 id
        self.assertEqual(self.read_status(), "in-progress")  # die 先于状态变更
        self.assertEqual(self.read_events(), [])  # 不产生任何事件

    def test_review_rejects_empty_and_none_reviewer(self):
        """未指定家族（空/none）同样拦截——复用 is_fast_path 的同一套"未指定"判定。"""
        for reviewer in ("", "none"):
            with self.subTest(reviewer=reviewer):
                self.write_task(reviewer=reviewer)
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        task.cmd_review(["TASK-101-test"])
                self.assertIn("fast-path", err.getvalue())
                self.assertEqual(self.read_status(), "in-progress")

    def test_review_with_specified_reviewer_transitions_and_writes_event(self):
        """指定 reviewer → 正常转 in-review，写既有 task.review_requested 事件（格式不变）。"""
        self.write_task(reviewer="alice")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            task.cmd_review(["TASK-101-test"])  # 不应退出
        self.assertEqual(self.read_status(), "in-review")
        events = self.read_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["ev"], "task.review_requested")
        self.assertEqual(events[0]["from"], "in-progress")
        self.assertEqual(events[0]["to"], "in-review")

    def test_review_p0_with_unspecified_reviewer_still_allowed(self):
        """P0 任务 reviewer=any 不拦截：is_fast_path 语义不变（P0 本就需要审查，循环不跳过）。"""
        self.write_task(reviewer="any", risk="P0")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            task.cmd_review(["TASK-101-test"])
        self.assertEqual(self.read_status(), "in-review")
        self.assertEqual(len(self.read_events()), 1)


if __name__ == "__main__":
    unittest.main()
