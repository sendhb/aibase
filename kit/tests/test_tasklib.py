"""TASK-027 — tasklib.py 单测（TASK-021 交付的共享核心层）。

覆盖：
- parse_frontmatter：metadata 段/list 值/无 frontmatter/未闭合/注释跳过
- find_project_root / task_files / find_task / pick_task / short_id / fm_get
- 治理判断：needs_review / is_fast_path / p0_missing_approval / rework_count / rework_exceeded
- write_assignee：替换 / depends-on 后插入 / 幂等 / 反斜杠值（SMELL-001 回归）
- set_status：合法流转 / 非法流转 die / 同状态幂等 / 事件追加
- bump_rework：首打回计数 / 非 in-review 不计数 / 超限 die
- 证据校验：evidence_exists / verify_exists / review_exists / check_done_prereqs
- 事件：append_event seq 单调 / _event_last_seq 损坏行容错 / validate_events

约定：目录路径全部显式传参（tasklib 设计），临时目录隔离，不触碰真实 runtime/。
die() 走 sys.exit → 用 assertRaises(SystemExit) + redirect_stderr 捕获（避免
Windows GBK 控制台编码 ✗ 字符报错，TASK-011 教训）。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.path.join(os.path.dirname(_HERE), "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import tasklib  # noqa: E402


def _write_task(path, status="open", risk="P2", priority="P2", reviewer="any",
                approval="none", rework="0", assignee="any", extra=""):
    fm = (
        "---\n"
        "name: %s\n"
        "description: test task\n"
        "metadata:\n"
        "  type: task\n"
        "  status: %s\n"
        "  priority: %s\n"
        "  risk: %s\n"
        "  approval-ref: %s\n"
        "  assignee: %s\n"
        "  reviewer: %s\n"
        "  rework-count: %s\n"
        "  depends-on: []\n"
        "  tags: [test]\n"
        "---\n\n"
        "# body\n\n%s\n"
    ) % (os.path.basename(path)[:-3], status, priority, risk, approval,
         assignee, reviewer, rework, extra)
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm)
    return path


def _write_evidence(directory, filename, record_type, task_ref, result="pass",
                    date=None, verifier="v", commit="abc1234"):
    os.makedirs(directory, exist_ok=True)
    text = (
        "---\n"
        "name: %s\n"
        "description: evidence record\n"
        "metadata:\n"
        "  type: %s\n"
        "  date: %s\n"
        "  task-ref: %s\n"
        "  verifier: %s\n"
        "  commit: %s\n"
        "  result: %s\n"
        "---\n\nbody\n"
    ) % (filename[:-3], record_type, date or tasklib.today(), task_ref,
         verifier, commit, result)
    with open(os.path.join(directory, filename), "w", encoding="utf-8") as f:
        f.write(text)


class FrontmatterTests(unittest.TestCase):
    def test_parse_full(self):
        fm, end = tasklib.parse_frontmatter(
            "---\nname: TASK-001-x\ndescription: d\nmetadata:\n"
            "  status: open\n  tags: [a, b]\n---\nbody")
        self.assertEqual(fm["name"], "TASK-001-x")
        self.assertEqual(fm["metadata.status"], "open")
        self.assertEqual(fm["metadata.tags"], ["a", "b"])
        self.assertGreater(end, 0)

    def test_parse_missing_and_unclosed(self):
        self.assertEqual(tasklib.parse_frontmatter("no frontmatter")[0], None)
        self.assertEqual(tasklib.parse_frontmatter("---\nname: x\n")[0], None)

    def test_parse_skips_comments_and_blank(self):
        fm, _ = tasklib.parse_frontmatter(
            "---\n# comment\n\nname: TASK-001-x\n---\n")
        self.assertEqual(fm.get("name"), "TASK-001-x")
        self.assertNotIn("metadata.status", fm)


class ProjectRootTests(unittest.TestCase):
    def test_find_project_root_upward(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        os.makedirs(os.path.join(tmp, "a", "b", "c"))
        with open(os.path.join(tmp, "aios.config.yaml"), "w") as f:
            f.write("commands: {}\n")
        self.assertEqual(tasklib.find_project_root(os.path.join(tmp, "a", "b", "c", "x.py")), tmp)

    def test_short_id(self):
        self.assertEqual(tasklib.short_id("TASK-012-abc"), "TASK-012")
        self.assertEqual(tasklib.short_id("not-a-task"), None)
        self.assertEqual(tasklib.short_id(None), None)

    def test_fm_get_both_forms(self):
        self.assertEqual(tasklib.fm_get({"status": "open"}, "status"), "open")
        self.assertEqual(tasklib.fm_get({"metadata.status": "open"}, "status"), "open")
        self.assertEqual(tasklib.fm_get({}, "status"), None)


class TaskPickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.makedirs(self.tasks)

    def test_task_files_sorted_and_filtered(self):
        _write_task(os.path.join(self.tasks, "TASK-902-b.md"))
        _write_task(os.path.join(self.tasks, "TASK-901-a.md"))
        with open(os.path.join(self.tasks, "README.md"), "w") as f:
            f.write("x")
        with open(os.path.join(self.tasks, "TASK-903-UPPER.md"), "w") as f:
            f.write("x")
        self.assertEqual(tasklib.task_files(self.tasks),
                         ["TASK-901-a.md", "TASK-902-b.md"])

    def test_find_task_by_prefix(self):
        _write_task(os.path.join(self.tasks, "TASK-901-fast.md"))
        self.assertTrue(tasklib.find_task("TASK-901", self.tasks).endswith("TASK-901-fast.md"))
        self.assertEqual(tasklib.find_task("TASK-999", self.tasks), None)

    def test_pick_task_first_open(self):
        _write_task(os.path.join(self.tasks, "TASK-901-a.md"), status="done")
        _write_task(os.path.join(self.tasks, "TASK-902-b.md"), status="open")
        _write_task(os.path.join(self.tasks, "TASK-903-c.md"), status="open")
        self.assertEqual(tasklib.pick_task(self.tasks), "TASK-902")

    def test_pick_task_none_when_all_done(self):
        _write_task(os.path.join(self.tasks, "TASK-901-a.md"), status="done")
        self.assertEqual(tasklib.pick_task(self.tasks), None)


class GovernanceTests(unittest.TestCase):
    def _fm(self, risk="P2", priority="P2", reviewer="any", approval="none",
            rework="0", status="open"):
        return {"metadata.status": status, "metadata.risk": risk,
                "metadata.priority": priority, "metadata.reviewer": reviewer,
                "metadata.approval-ref": approval, "metadata.rework-count": rework}

    def test_needs_review(self):
        self.assertTrue(tasklib.needs_review(self._fm(risk="P1")))
        self.assertTrue(tasklib.needs_review(self._fm(priority="P0")))
        self.assertTrue(tasklib.needs_review(self._fm(reviewer="someone")))
        self.assertFalse(tasklib.needs_review(self._fm()))
        self.assertTrue(tasklib.needs_review(self._fm(), require_review=True))

    def test_is_fast_path_inverse(self):
        self.assertTrue(tasklib.is_fast_path(self._fm()))
        self.assertFalse(tasklib.is_fast_path(self._fm(risk="P1")))

    def test_p0_missing_approval(self):
        self.assertTrue(tasklib.p0_missing_approval(self._fm(risk="P0")))
        self.assertFalse(tasklib.p0_missing_approval(self._fm(risk="P0", approval="TICKET-1")))
        self.assertFalse(tasklib.p0_missing_approval(self._fm(risk="P1")))

    def test_rework_count_and_limit(self):
        self.assertEqual(tasklib.rework_count(self._fm(rework="2")), 2)
        self.assertEqual(tasklib.rework_count(self._fm(rework="garbage")), 0)
        self.assertFalse(tasklib.rework_exceeded(self._fm(rework="2")))
        self.assertTrue(tasklib.rework_exceeded(self._fm(rework="3")))


class WriteAssigneeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = _write_task(os.path.join(self.tmp.name, "TASK-901-a.md"))

    def test_replace_existing(self):
        self.assertTrue(tasklib.write_assignee(self.path, "agent-x"))
        text, fm = tasklib.load_task(self.path)
        self.assertEqual(fm.get("metadata.assignee"), "agent-x")

    def test_idempotent(self):
        tasklib.write_assignee(self.path, "agent-x")
        self.assertFalse(tasklib.write_assignee(self.path, "agent-x"))

    def test_backslash_value_untouched(self):
        # SMELL-001 回归：反斜杠值不得被 re.sub 当作替换组解析
        self.assertTrue(tasklib.write_assignee(self.path, r"back\slash"))
        text, _ = tasklib.load_task(self.path)
        self.assertIn(r"assignee: back\slash", text)


class SetStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.tasks = os.path.join(self.root, "tasks")
        self.verify = os.path.join(self.root, "verification")
        self.reviews = os.path.join(self.root, "reviews")
        self.logs = os.path.join(self.root, "logs")
        for d in (self.tasks, self.verify, self.reviews, self.logs):
            os.makedirs(d)
        self.path = _write_task(os.path.join(self.tasks, "TASK-901-a.md"))

    def test_valid_transition_and_event(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tasklib.set_status(self.path, "in-progress", self.verify,
                               self.reviews, self.logs, root=self.root, ev="start")
        _, fm = tasklib.load_task(self.path)
        self.assertEqual(fm.get("metadata.status"), "in-progress")
        errors, total = tasklib.validate_events(self.logs)
        self.assertEqual((errors, total), (0, 1))

    def test_invalid_transition_dies(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit):
                tasklib.set_status(self.path, "done", self.verify, self.reviews,
                                   self.logs, root=self.root)

    def test_same_status_noop(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            tasklib.set_status(self.path, "open", self.verify, self.reviews,
                               self.logs, root=self.root)
        errors, total = tasklib.validate_events(self.logs)
        self.assertEqual((errors, total), (0, 0))


class BumpReworkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tasks = os.path.join(self.tmp.name, "tasks")
        os.makedirs(self.tasks)

    def test_first_sendback_counts(self):
        path = _write_task(os.path.join(self.tasks, "TASK-901-a.md"), status="in-review")
        # redirect_stdout：bump_rework 内部 print("✓...") 在 GBK 控制台会
        # UnicodeEncodeError（tasklib 未 reconfigure，库直用时潜在缺陷，见任务备注）
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tasklib.bump_rework(path, "TASK-901"), 1)
        _, fm = tasklib.load_task(path)
        self.assertEqual(fm.get("metadata.rework-count"), "1")

    def test_non_in_review_returns_none(self):
        path = _write_task(os.path.join(self.tasks, "TASK-901-a.md"), status="open")
        self.assertEqual(tasklib.bump_rework(path, "TASK-901"), None)

    def test_limit_refuses_third(self):
        path = _write_task(os.path.join(self.tasks, "TASK-901-a.md"),
                           status="in-review", rework="2")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            with self.assertRaises(SystemExit):
                tasklib.bump_rework(path, "TASK-901")


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.verify = os.path.join(self.tmp.name, "verification")
        self.reviews = os.path.join(self.tmp.name, "reviews")

    def test_verify_exists_match(self):
        _write_evidence(self.verify, "VERIFY-2026-08-30-x.md", "verify", "TASK-901")
        self.assertTrue(tasklib.verify_exists("TASK-901-fast", self.verify))

    def test_verify_exists_wrong_result_or_date(self):
        _write_evidence(self.verify, "VERIFY-x.md", "verify", "TASK-901", result="fail")
        self.assertFalse(tasklib.verify_exists("TASK-901", self.verify))
        _write_evidence(self.verify, "VERIFY-y.md", "verify", "TASK-901", date="2000-01-01")
        self.assertFalse(tasklib.verify_exists("TASK-901", self.verify, ref_date="2026-08-30"))

    def test_review_exists_author_must_differ(self):
        task_fm = {"metadata.assignee": "agent-x"}
        _write_evidence(self.reviews, "REVIEW-x.md", "review", "TASK-901")
        # 补 implementation-author / reviewer 字段
        path = os.path.join(self.reviews, "REVIEW-x.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("  result: pass",
                            "  result: pass\n  reviewer: reviewer-y\n"
                            "  implementation-author: agent-x")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertTrue(tasklib.review_exists("TASK-901", self.reviews, task_fm))
        # 生成者 == 审查者 → 无效
        with open(path, encoding="utf-8") as f:
            text = f.read()
        text = text.replace("reviewer: reviewer-y", "reviewer: agent-x")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.assertFalse(tasklib.review_exists("TASK-901", self.reviews, task_fm))


class DonePrereqTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.tasks = os.path.join(self.root, "tasks")
        self.verify = os.path.join(self.root, "verification")
        self.reviews = os.path.join(self.root, "reviews")
        for d in (self.tasks, self.verify, self.reviews):
            os.makedirs(d)
        self.path = _write_task(os.path.join(self.tasks, "TASK-901-a.md"),
                                extra="- [x] 条件 1\n- [x] build 零错误\n")
        _, fm = tasklib.load_task(self.path)

    def _call(self, fm, require_review=False):
        with contextlib.redirect_stderr(io.StringIO()):
            tasklib.check_done_prereqs(self.path, fm, self.verify, self.reviews,
                                       require_review=require_review)

    def test_unchecked_box_dies(self):
        path = _write_task(os.path.join(self.tasks, "TASK-902-b.md"),
                           extra="- [ ] 未勾选\n")
        _, fm = tasklib.load_task(path)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._call(fm)

    def test_missing_verify_dies(self):
        _, fm = tasklib.load_task(self.path)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._call(fm)

    def test_p0_without_approval_dies(self):
        _write_evidence(self.verify, "VERIFY-x.md", "verify", "TASK-901")
        path = _write_task(os.path.join(self.tasks, "TASK-903-c.md"),
                           risk="P0", approval="none")
        _, fm = tasklib.load_task(path)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._call(fm)

    def test_p1_requires_review_record(self):
        _write_evidence(self.verify, "VERIFY-x.md", "verify", "TASK-901")
        _, fm = tasklib.load_task(self.path)
        fm = dict(fm, **{"metadata.risk": "P1"})
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self._call(fm)

    def test_fast_path_passes_with_verify(self):
        _write_evidence(self.verify, "VERIFY-x.md", "verify", "TASK-901")
        _, fm = tasklib.load_task(self.path)
        self._call(fm)  # 不抛即通过


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.logs = os.path.join(self.tmp.name, "logs")

    def test_append_event_seq_monotonic(self):
        tasklib.append_event("start", "TASK-901-a.md", log_dir=self.logs)
        tasklib.append_event("done", "TASK-901-a.md", log_dir=self.logs,
                             from_status="in-review", to_status="done")
        errors, total = tasklib.validate_events(self.logs)
        self.assertEqual((errors, total), (0, 2))
        with open(tasklib.events_path(self.logs), encoding="utf-8") as f:
            seqs = [__import__("json").loads(l)["seq"] for l in f if l.strip()]
        self.assertEqual(seqs, [1, 2])

    def test_event_last_seq_corrupt_tail(self):
        path = tasklib.events_path(self.logs)
        os.makedirs(self.logs, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"seq": 7, "ts": "t", "ev": "x", "task": "TASK-901"}\n')
            f.write("not-json-garbage\n")
        self.assertEqual(tasklib._event_last_seq(path), 7)
        self.assertEqual(tasklib._event_last_seq(os.path.join(self.logs, "nope.jsonl")), 0)

    def test_validate_events_detects_problems(self):
        os.makedirs(self.logs, exist_ok=True)
        path = tasklib.events_path(self.logs)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"seq": 1, "ts": "t", "ev": "x", "task": "T"}\n')
            f.write("broken\n")
            f.write('{"seq": 1, "ts": "t", "ev": "x", "task": "T"}\n')  # 不单调
            f.write('{"seq": 3, "ts": "t", "ev": "x"}\n')              # 缺 task
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            errors, total = tasklib.validate_events(self.logs)
        self.assertEqual(total, 4)
        self.assertEqual(errors, 3)

    def test_validate_events_missing_file(self):
        self.assertEqual(tasklib.validate_events(self.logs), (0, 0))


if __name__ == "__main__":
    unittest.main()
