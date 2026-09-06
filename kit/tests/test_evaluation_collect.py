"""TASK-093 — 评估自动采集单测。

覆盖：
- evaluation_dir：kit 布局 / 平铺布局两种解析
- collect_done_metrics：字段齐全合法 JSON、duration_days 计算、字段缺失容错、写盘失败告警
- set_status done 钩子：metrics 末行含 task/risk/rework-count/done_at；采集失败不阻塞 done（stderr 告警）
- collect_verify_failure + cmd_verify 失败路径：failures.jsonl 记录、fail_log 指向存在日志；写盘失败不阻塞 die
- cmd_metrics：done 率/平均 rework-count 数值恒输出、metrics 优先与实时回退、token 聚合 / N/A 容错 / --days 过滤

约定（沿 test_tasklib.py / test_task_verify_streaming.py）：目录显式传参，
临时目录隔离，不触碰真实 runtime/ 与 kit/evaluation/；die() 用
assertRaises(SystemExit) + redirect_stderr 捕获。
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_HERE), "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import tasklib  # noqa: E402（同 test_tasklib.py：显式把 cli/lib 加进 sys.path）

_TASK_PATH = "kit/cli/task" if os.path.isfile("kit/cli/task") else "cli/task"
loader = importlib.machinery.SourceFileLoader("task_cli_eval", _TASK_PATH)
spec = importlib.util.spec_from_loader(loader.name, loader)
task_cli = importlib.util.module_from_spec(spec)
loader.exec_module(task_cli)


def _mk_task(path, status="in-progress", rework=2, reviewer="any", created="2026-09-01",
             updated="2026-09-03"):
    Path(path).write_text(
        "---\n"
        "name: %s\n"
        "description: sandbox\n"
        "metadata:\n"
        "  type: task\n"
        "  status: %s\n"
        "  priority: P1\n"
        "  risk: P2\n"
        "  approval-ref: none\n"
        "  assignee: any\n"
        "  reviewer: %s\n"
        "  rework-count: %d\n"
        "created: %s\n"
        "updated: %s\n"
        "---\n\n# body（无未勾选框）\n" % (Path(path).stem, status, reviewer, rework,
                                          created, updated),
        encoding="utf-8")


def _mk_evidence(root, task_id="TASK-901"):
    """合法 VERIFY + REVIEW 证据（reviewer ≠ implementation-author，满足 done 前置）。

    日期动态取 tasklib.today()：evidence_exists 要求 metadata.date == 运行日，
    硬编码日期会在跨日翻转后全线失败（2026-09-05 实录，TASK-097 顺带修复）。
    """
    today = tasklib.today()
    vdir = Path(root) / "runtime/verification"
    rdir = Path(root) / "runtime/reviews"
    vdir.mkdir(parents=True, exist_ok=True)
    rdir.mkdir(parents=True, exist_ok=True)
    (vdir / f"VERIFY-{today}-{task_id.lower()}.md").write_text(
        "---\n"
        f"name: VERIFY-{today}-{task_id.lower()}\n"
        "description: sandbox verify\n"
        "metadata:\n"
        "  type: verify\n"
        f"  date: {today}\n"
        f"  task-ref: {task_id}\n"
        "  verifier: sandbox\n"
        "  result: pass\n"
        "  commit: abc1234\n"
        "---\n", encoding="utf-8")
    (rdir / f"REVIEW-{today}-{task_id.lower()}.md").write_text(
        "---\n"
        f"name: REVIEW-{today}-{task_id.lower()}\n"
        "description: sandbox review\n"
        "metadata:\n"
        "  type: review\n"
        f"  date: {today}\n"
        f"  task-ref: {task_id}\n"
        "  reviewer: alice\n"
        "  implementation-author: bob\n"
        "  result: pass\n"
        "---\n", encoding="utf-8")


class EvaluationDirTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_flat_layout(self):
        self.assertEqual(tasklib.evaluation_dir(self.root),
                         os.path.join(self.root, "evaluation"))

    def test_kit_layout(self):
        os.mkdir(os.path.join(self.root, "kit"))
        self.assertEqual(tasklib.evaluation_dir(self.root),
                         os.path.join(self.root, "kit", "evaluation"))


class CollectDoneMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.path = os.path.join(self.root, "TASK-901-demo.md")
        _mk_task(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _fm(self):
        return tasklib.load_task(self.path)[1]

    def test_fields_and_duration(self):
        fm = self._fm()
        self.assertTrue(tasklib.collect_done_metrics(self.root, self.path, fm))
        mf = Path(self.root) / "evaluation/metrics/task-metrics.jsonl"
        line = mf.read_text(encoding="utf-8").strip().splitlines()[-1]
        r = json.loads(line)
        self.assertEqual(r["task"], "TASK-901-demo")
        self.assertEqual(r["risk"], "P2")
        self.assertEqual(r["priority"], "P1")
        self.assertEqual(r["rework-count"], 2)
        self.assertEqual(r["duration_days"], 2)
        self.assertEqual(r["done_at"], tasklib.today())

    def test_missing_fields_degrade(self):
        fm = {"name": "TASK-902-bare"}
        self.assertTrue(tasklib.collect_done_metrics(self.root, self.path, fm))
        mf = Path(self.root) / "evaluation/metrics/task-metrics.jsonl"
        r = json.loads(mf.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(r["rework-count"], 0)
        self.assertEqual(r["duration_days"], "")
        self.assertEqual(r["risk"], "")

    def test_write_failure_warns_not_raises(self):
        os.makedirs(os.path.join(self.root, "evaluation"))
        open(os.path.join(self.root, "evaluation", "metrics"), "w").close()  # 用同名文件挡路
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = tasklib.collect_done_metrics(self.root, self.path, self._fm())
        self.assertFalse(ok)
        self.assertIn("评估采集写盘失败", err.getvalue())


class SetStatusDoneHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.rtask = os.path.join(self.root, "runtime/tasks")
        self.verify = os.path.join(self.root, "runtime/verification")
        self.reviews = os.path.join(self.root, "runtime/reviews")
        self.logs = os.path.join(self.root, "runtime/logs")
        for d in (self.rtask, self.verify, self.reviews, self.logs):
            os.makedirs(d)
        self.path = os.path.join(self.rtask, "TASK-901-demo.md")
        _mk_task(self.path, reviewer="alice")
        _mk_evidence(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def _set_done(self):
        tasklib.set_status(self.path, "done", self.verify, self.reviews,
                           self.logs, root=self.root)

    def test_done_path_writes_metrics_last_line(self):
        self._set_done()
        mf = Path(self.root) / "evaluation/metrics/task-metrics.jsonl"
        last = mf.read_text(encoding="utf-8").strip().splitlines()[-1]
        r = json.loads(last)  # 末行必须是合法 JSON
        for field in ("task", "risk", "rework-count", "done_at"):
            self.assertIn(field, r)
        self.assertEqual(r["task"], "TASK-901-demo")
        self.assertEqual(r["rework-count"], 2)
        self.assertIn("status: done", Path(self.path).read_text(encoding="utf-8"))

    def test_collect_failure_does_not_block_done(self):
        os.makedirs(os.path.join(self.root, "evaluation"))
        open(os.path.join(self.root, "evaluation", "metrics"), "w").close()  # 挡路
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._set_done()  # 不应抛异常
        self.assertIn("status: done", Path(self.path).read_text(encoding="utf-8"))
        self.assertIn("评估采集", err.getvalue())


class CollectVerifyFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_direct_fields(self):
        ok = tasklib.collect_verify_failure(self.root, "TASK-903", "exit 2",
                                            "runtime/logs/fail-2026-09-04.log")
        self.assertTrue(ok)
        fj = Path(self.root) / "evaluation/failures/failures.jsonl"
        r = json.loads(fj.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(r["task"], "TASK-903")
        self.assertEqual(r["date"], tasklib.today())
        self.assertEqual(r["failed_command"], "exit 2")
        self.assertEqual(r["fail_log"], "runtime/logs/fail-2026-09-04.log")

    def test_write_failure_warns_not_raises(self):
        os.makedirs(os.path.join(self.root, "evaluation"))
        open(os.path.join(self.root, "evaluation", "failures"), "w").close()  # 挡路
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ok = tasklib.collect_verify_failure(self.root, "TASK-903", "exit 2", "x.log")
        self.assertFalse(ok)
        self.assertIn("评估采集写盘失败", err.getvalue())


class CmdVerifyFailureCollectTests(unittest.TestCase):
    """验收 #2：命令替身返回非零 → failures.jsonl 出记录且 fail_log 指向存在日志。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = (task_cli.AI_DIR, task_cli.TASKS_DIR, task_cli.LOG_DIR,
                      task_cli.VERIFY_DIR, task_cli.cfg_lib.load_config)
        task_cli.AI_DIR = str(root)
        task_cli.TASKS_DIR = str(root / "runtime/tasks")
        task_cli.LOG_DIR = str(root / "runtime/logs")
        task_cli.VERIFY_DIR = str(root / "runtime/verification")
        task_cli.cfg_lib.load_config = lambda ai_dir: {
            "commands": {"build": "true", "lint": "exit 2",
                         "test": "true", "check": "true"}}
        (root / "runtime/tasks").mkdir(parents=True)
        (root / "runtime/tasks/TASK-902-faildemo.md").write_text(
            "---\nname: TASK-902-faildemo\ndescription: x\n"
            "metadata:\n  type: task\n  status: in-progress\n---\n# b\n",
            encoding="utf-8")

    def tearDown(self):
        (task_cli.AI_DIR, task_cli.TASKS_DIR, task_cli.LOG_DIR,
         task_cli.VERIFY_DIR, task_cli.cfg_lib.load_config) = self._orig
        self.tmp.cleanup()

    def test_failure_writes_failures_jsonl(self):
        with self.assertRaises(SystemExit):
            task_cli.cmd_verify(["TASK-902"])
        fj = Path(self.tmp.name) / "evaluation/failures/failures.jsonl"
        r = json.loads(fj.read_text(encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(r["task"], "TASK-902")
        self.assertEqual(r["failed_command"], "exit 2")
        self.assertIn("fail-", r["fail_log"])
        logpath = Path(self.tmp.name) / r["fail_log"]
        self.assertTrue(logpath.is_file())          # 指向存在的日志路径模式
        self.assertIn("exit 2", logpath.read_text(encoding="utf-8"))

    def test_collect_failure_does_not_block_die(self):
        os.makedirs(os.path.join(self.tmp.name, "evaluation"))
        open(os.path.join(self.tmp.name, "evaluation", "failures"), "w").close()  # 挡路
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            task_cli.cmd_verify(["TASK-902"])
        self.assertIn("未通过", err.getvalue())      # die 语义不变
        self.assertTrue(list(Path(self.tmp.name, "runtime/logs").glob("fail-*.log")))


class CmdMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self._orig = (task_cli.AI_DIR, task_cli.TASKS_DIR, task_cli.LOG_DIR)
        task_cli.AI_DIR = str(root)
        task_cli.TASKS_DIR = str(root / "runtime/tasks")
        task_cli.LOG_DIR = str(root / "runtime/logs")
        (root / "runtime/tasks").mkdir(parents=True)
        (root / "runtime/logs").mkdir()

    def tearDown(self):
        (task_cli.AI_DIR, task_cli.TASKS_DIR, task_cli.LOG_DIR) = self._orig
        self.tmp.cleanup()

    def _mk(self, name, status, rework):
        _mk_task(os.path.join(self.tmp.name, "runtime/tasks", f"{name}.md"),
                 status=status, rework=rework)

    def _run(self, args=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            task_cli.cmd_metrics(list(args))  # 不抛异常 = 退出码 0
        return out.getvalue()

    def test_empty_data_na_and_numbers(self):
        self._mk("TASK-801-a", "done", 1)
        self._mk("TASK-802-b", "in-progress", 0)
        self._mk("TASK-803-c", "open", 2)
        txt = self._run()
        self.assertIn("done 率: 1/3 (33.3%)", txt)
        self.assertIn("平均 rework-count: 1.00", txt)      # 实时回退口径 (1+0+2)/3
        self.assertIn("token 估算合计: N/A", txt)          # 缺失文件容错

    def test_metrics_priority_and_token_aggregate(self):
        self._mk("TASK-801-a", "done", 9)
        m = Path(self.tmp.name) / "evaluation/metrics"
        m.mkdir(parents=True)
        (m / "task-metrics.jsonl").write_text(
            json.dumps({"task": "A", "rework-count": 3, "done_at": tasklib.today()}) + "\n"
            + json.dumps({"task": "B", "rework-count": 1, "done_at": tasklib.today()}) + "\n",
            encoding="utf-8")
        tu = Path(self.tmp.name) / "runtime/logs/token-usage.jsonl"
        tu.write_text(json.dumps({"ts": time.time(), "token_estimate": 100}) + "\n"
                      + json.dumps({"ts": time.time(), "token_estimate": 250}) + "\n",
                      encoding="utf-8")
        txt = self._run()
        self.assertIn("平均 rework-count: 2.00（来源: metrics/task-metrics.jsonl）", txt)
        self.assertIn("token 估算合计: 350", txt)

    def test_days_filters_old_tokens(self):
        tu = Path(self.tmp.name) / "runtime/logs/token-usage.jsonl"
        tu.write_text(json.dumps({"ts": 0, "token_estimate": 999}) + "\n", encoding="utf-8")
        txt = self._run(["--days", "7"])
        self.assertIn("token 估算合计: 0（近 7 天）", txt)

    def test_bad_jsonl_line_skipped(self):
        self._mk("TASK-801-a", "done", 0)
        m = Path(self.tmp.name) / "evaluation/metrics"
        m.mkdir(parents=True)
        (m / "task-metrics.jsonl").write_text("not-json\n" +
                                              json.dumps({"task": "A", "rework-count": 1,
                                                          "done_at": tasklib.today()}) + "\n",
                                              encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            txt = self._run()
        self.assertIn("平均 rework-count: 1.00（来源: metrics/task-metrics.jsonl）", txt)
        self.assertIn("非合法 JSON", err.getvalue())

    def test_bad_days_dies(self):
        with self.assertRaises(SystemExit):
            self._run(["--days"])
        with self.assertRaises(SystemExit):
            self._run(["--days", "abc"])


if __name__ == "__main__":
    unittest.main()
