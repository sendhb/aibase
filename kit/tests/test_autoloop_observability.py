"""TASK-099 — autoloop 可观测性收口单测。

覆盖：
- autoloop_launcher.cmd_status：四要素聚合（空仓 / 伪造 PID+heartbeat+任务+事件）
- 孤儿收编：record_llm_pid / clear_llm_pid / reap_llm_pid（真实子进程 SIGTERM 路径）
- kit/cli/task：cmd_block 原因必填（缺失/空白 → SystemExit(1)）、
  完整 block 流程（tmp 根覆写模块目录变量：任务转 blocked + BLOCKERS.md + 事件追加）
- 并发 task new：多进程并行创建后编号互异、task-events.jsonl seq 严格递增且
  validate_events 零错误（2026-09-04 双 174 实录的回归测试）

**安全边界**：不触发真实 LLM（reap 用 sleep 子进程；task new/block 不调 LLM）；
cmd_block 完整流程经 tmp 根覆写模块级目录变量隔离，不碰真实 runtime/。
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_REPO = os.path.dirname(_KIT)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import autoloop_launcher as L  # noqa: E402


def _load_task_cli():
    """以独立模块名加载 kit/cli/task（无 .py 后缀脚本，需显式 SourceFileLoader）。"""
    from importlib.machinery import SourceFileLoader
    path = os.path.join(_KIT, "cli", "task")
    loader = SourceFileLoader("task_cli_test099", path)
    spec = importlib.util.spec_from_loader("task_cli_test099", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _make_tmp_root():
    """建最小 tmp 项目根（aios.config.yaml + runtime 结构 + 任务模板）。"""
    root = tempfile.mkdtemp(prefix="t099-")
    for d in ("runtime/tasks", "runtime/logs", "runtime/locks", "runtime/states"):
        os.makedirs(os.path.join(root, d))
    with open(os.path.join(root, "aios.config.yaml"), "w", encoding="utf-8") as f:
        f.write("project: t099-test\n")
    # 最小任务模板（cmd_new 替换占位符所需字段齐全）
    with open(os.path.join(root, "runtime", "tasks", "TASK.template.md"), "w",
              encoding="utf-8") as f:
        f.write("---\nname: TASK-000-<slug>\ndescription: 一句话说明任务目标\n"
                "metadata:\n  type: task\n  status: open\n  created: YYYY-MM-DD\n"
                "  updated: YYYY-MM-DD\n  priority: P2\n  risk: P2\n"
                "  approval-ref: none\n  assignee: any\n  reviewer: any\n"
                "  parent: TASK-000\n  depends-on: []\n  rework-count: 0\n---\n\n"
                "# TASK-000 — 任务标题\n")
    return root


def _write_task_file(root, task_id, status="open"):
    """写一个最小任务文件（cmd_block 完整流程用）。"""
    path = os.path.join(root, "runtime", "tasks", f"{task_id}-test.md")
    now = time.strftime("%Y-%m-%d")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"---\nname: {task_id}-test\ndescription: 测试\n"
                f"metadata:\n  type: task\n  status: {status}\n  created: {now}\n"
                f"  updated: {now}\n  priority: P2\n  risk: P2\n"
                f"  approval-ref: none\n  assignee: any\n  reviewer: any\n"
                f"  parent: TASK-000\n  depends-on: []\n  rework-count: 0\n---\n\n"
                f"# {task_id} — 测试\n")
    return path


class TestStatusCommand(unittest.TestCase):
    """cmd_status 四要素聚合。"""

    def setUp(self):
        self.root = _make_tmp_root()
        self.p = L.Parsed()  # interval=None → both_interval 默认

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self):
        out = __import__("io").StringIO()
        err = __import__("io").StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = L.cmd_status(self.root, self.p)
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(rc, 0)
        return out.getvalue() + err.getvalue()

    def test_empty_root_four_sections(self):
        text = self._run()
        self.assertIn("① 壳 both", text)
        self.assertIn("已停止", text)
        self.assertIn("② LLM 子进程", text)
        self.assertIn("③ in-progress 任务", text)
        self.assertIn("④ 最近事件", text)

    def test_running_daemon_and_task_and_events(self):
        # ① both PID 文件（活进程 = 本测试进程）
        with open(os.path.join(self.root, "runtime", "locks", L.BOTH_PID), "w") as f:
            f.write(str(os.getpid()))
        # 心跳文件（mtime = 现在 → 新鲜）
        hb = os.path.join(self.root, "runtime", "logs", "autoloop-coder.heartbeat")
        with open(hb, "w") as f:
            f.write(str(int(time.time())))
        # ③ in-progress 任务
        _write_task_file(self.root, "TASK-042", status="in-progress")
        # ④ 事件
        ev = os.path.join(self.root, "runtime", "logs", "task-events.jsonl")
        with open(ev, "w", encoding="utf-8") as f:
            f.write(json.dumps({"seq": 1, "ev": "task.created", "task": "TASK-042"}) + "\n")
        text = self._run()
        self.assertIn("运行中（PID %d）" % os.getpid(), text)
        self.assertIn("新鲜", text)
        self.assertIn("TASK-042-test", text)
        self.assertIn('"seq": 1', text)

    def test_stale_heartbeat_flagged(self):
        hb = os.path.join(self.root, "runtime", "logs", "autoloop-coder.heartbeat")
        with open(hb, "w") as f:
            f.write(str(int(time.time())))
        old = time.time() - 99999
        os.utime(hb, (old, old))
        text = self._run()
        self.assertIn("停滞", text)

    def test_orphan_llm_flagged_by_pid_record(self):
        rec = {"pid": 999999999, "llm": "pi", "task": "TASK-042", "ts": 0}
        with open(os.path.join(self.root, "runtime", "locks", L.LLM_PID), "w",
                  encoding="utf-8") as f:
            json.dump(rec, f)
        text = self._run()
        self.assertIn("已死（PID 记录残留）", text)


class TestReap(unittest.TestCase):
    """LLM 子进程 PID 记录与收编（真实子进程，无 LLM 调用）。"""

    def setUp(self):
        self.root = _make_tmp_root()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_record_read_clear_cycle(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            L.record_llm_pid(self.root, p.pid, "pi", "TASK-099")
            rec = L.read_llm_pid(self.root)
            self.assertEqual(rec["pid"], p.pid)
            self.assertEqual(rec["llm"], "pi")
            self.assertEqual(rec["task"], "TASK-099")
        finally:
            p.kill()
            p.wait()
        L.clear_llm_pid(self.root, pid=p.pid)
        self.assertIsNone(L.read_llm_pid(self.root))

    def test_clear_pid_mismatch_keeps_record(self):
        subprocess.Popen(["true"]).wait()
        L.record_llm_pid(self.root, 424242, "pi", "TASK-A")
        L.clear_llm_pid(self.root, pid=1111)  # pid 不匹配 → 不清
        self.assertIsNotNone(L.read_llm_pid(self.root))
        L.clear_llm_pid(self.root)  # 无条件清
        self.assertIsNone(L.read_llm_pid(self.root))

    def test_reap_kills_alive_child(self):
        p = subprocess.Popen(["sleep", "60"])
        L.record_llm_pid(self.root, p.pid, "claude", "TASK-100")
        self.assertTrue(L.reap_llm_pid(self.root, why="单测收编"))
        p.wait(timeout=10)
        self.assertIsNone(L.read_llm_pid(self.root))

    def test_reap_without_record_returns_false(self):
        self.assertFalse(L.reap_llm_pid(self.root))

    def test_reap_dead_pid_cleans_stale_record(self):
        p = subprocess.Popen(["true"])
        p.wait()  # 已死
        L.record_llm_pid(self.root, p.pid, "pi", "TASK-B")  # 模拟残留
        self.assertFalse(L.reap_llm_pid(self.root))
        self.assertIsNone(L.read_llm_pid(self.root))


class TestBlockRequiresReason(unittest.TestCase):
    """cmd_block 原因必填 + tmp 根完整流程。"""

    def setUp(self):
        self.root = _make_tmp_root()
        self.m = _load_task_cli()
        # 覆写模块级目录变量（头部注释明确支持测试覆写）
        self.m.AI_DIR = self.root
        self.m.RUNTIME_DIR = os.path.join(self.root, "runtime")
        self.m.TASKS_DIR = os.path.join(self.root, "runtime", "tasks")
        self.m.STATE_DIR = os.path.join(self.root, "runtime", "states")
        self.m.LOG_DIR = os.path.join(self.root, "runtime", "logs")
        self.m.EVENTS_LOCK = os.path.join(self.root, "runtime", "locks",
                                          "task-events.lock")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_reason_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.m.cmd_block(["TASK-042"])
        self.assertEqual(ctx.exception.code, 1)

    def test_blank_reason_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.m.cmd_block(["TASK-042", "   "])
        self.assertEqual(ctx.exception.code, 1)

    def test_unknown_task_exits_nonzero(self):
        with self.assertRaises(SystemExit):
            self.m.cmd_block(["TASK-998", "原因"])

    def test_full_block_flow(self):
        _write_task_file(self.root, "TASK-042")
        self.m.cmd_block(["TASK-042", "等待上游交付"])
        with open(os.path.join(self.root, "runtime", "tasks", "TASK-042-test.md"),
                  encoding="utf-8") as f:
            self.assertIn("status: blocked", f.read())
        blockers = os.path.join(self.root, "runtime", "states", "BLOCKERS.md")
        with open(blockers, encoding="utf-8") as f:
            self.assertIn("等待上游交付", f.read())
        ev = os.path.join(self.root, "runtime", "logs", "task-events.jsonl")
        with open(ev, encoding="utf-8") as f:
            lines = [json.loads(x) for x in f if x.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ev"], "task.blocked")
        self.assertEqual(lines[0]["reason"], "等待上游交付")
        self.assertEqual(lines[0]["seq"], 1)


class TestConcurrentTaskNew(unittest.TestCase):
    """多进程并行 task new：编号互异 + seq 严格递增（双 174 实录回归）。"""

    def test_parallel_new_no_seq_collision(self):
        root = _make_tmp_root()
        cli_dir = os.path.join(root, "cli")
        os.makedirs(cli_dir)
        shutil.copy(os.path.join(_KIT, "cli", "task"),
                    os.path.join(cli_dir, "task"))
        os.symlink(_LIB, os.path.join(cli_dir, "lib"))
        n = 4
        procs = [
            subprocess.Popen(
                [sys.executable, os.path.join(cli_dir, "task"),
                 "new", "并发任务%d" % i, "--slug", "conc-%d" % i],
                cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for i in range(n)
        ]
        for p in procs:
            _, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0,
                             "task new 失败: %s" % err.decode("utf-8", "replace"))
        # 编号互异（4 个文件）
        files = [f for f in os.listdir(os.path.join(root, "runtime", "tasks"))
                 if f.startswith("TASK-")]
        self.assertEqual(len(files), n)
        # seq 严格递增且 validate 零错误
        ev = os.path.join(root, "runtime", "logs", "task-events.jsonl")
        with open(ev, encoding="utf-8") as f:
            seqs = [json.loads(x)["seq"] for x in f if x.strip()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(set(seqs)), len(seqs), "seq 重复！")
        import tasklib
        errors, total = tasklib.validate_events(os.path.join(root, "runtime", "logs"))
        self.assertEqual(errors, 0)
        self.assertEqual(total, n)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
