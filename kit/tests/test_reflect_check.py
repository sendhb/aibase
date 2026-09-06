"""TASK-096 — reflect_check 单测（技术债标记扫描 / 越界检查 / "无"输出）。

场景全部用 diff 文本替身注入 scan()/render()，不依赖真实 git 状态（TASK-096
验收标准 1-3）；真实仓库可运行性由验收标准 4 的手工命令覆盖。
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import reflect_check as RC  # noqa: E402

SOURCE_DIRS = ["kit/cli/", "kit/aios/", "runtime/"]
GENERATED_DIRS = ["kit/cli/__pycache__/", "build/gen/"]

# 双 hunk 替身：第二个 hunk 验证新行号计数器跨 hunk 正确推进
DIFF_WITH_TODO = """diff --git a/kit/cli/lib/foo.py b/kit/cli/lib/foo.py
index 1111111..2222222 100644
--- a/kit/cli/lib/foo.py
+++ b/kit/cli/lib/foo.py
@@ -1,3 +1,4 @@
 context line one
+def hacky(): pass  # TODO: 只是先让它跑起来
 context line two
@@ -10,2 +11,3 @@
 old context
+FIXME: error handling missing here
 new context
"""

CLEAN_DIFF = """diff --git a/kit/cli/lib/bar.py b/kit/cli/lib/bar.py
index 3333333..4444444 100644
--- a/kit/cli/lib/bar.py
+++ b/kit/cli/lib/bar.py
@@ -1,2 +1,3 @@
 context
+clean added line without any markers
"""

GEN_FILE_DIFF = """diff --git a/kit/cli/__pycache__/foo.cpython-311.pyc b/kit/cli/__pycache__/foo.cpython-311.pyc
index 5555555..6666666 100644
--- a/kit/cli/__pycache__/foo.cpython-311.pyc
+++ b/kit/cli/__pycache__/foo.cpython-311.pyc
@@ -1 +1,2 @@
+manual edit inside generated dir
"""

OUTSIDE_SOURCE_DIFF = """diff --git a/other/random.py b/other/random.py
index 7777777..8888888 100644
--- a/other/random.py
+++ b/other/random.py
@@ -1 +1,2 @@
+added outside source_dirs
"""


def _tech_debt(findings):
    return [f for f in findings if f["type"] == "tech-debt"]


def _out_of_bounds(findings):
    return [f for f in findings if f["type"] == "out-of-bounds"]


class TechDebtScanTests(unittest.TestCase):
    def test_todo_hit_reports_file_and_line(self):
        findings = RC.scan(DIFF_WITH_TODO, SOURCE_DIRS, GENERATED_DIRS)
        hits = [f for f in _tech_debt(findings) if f["tag"] == "TODO"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "kit/cli/lib/foo.py")
        self.assertEqual(hits[0]["line"], 2)  # 第一个 hunk +c=1 的下一行
        self.assertIn("TODO", hits[0]["text"])

    def test_line_number_across_hunks(self):
        findings = RC.scan(DIFF_WITH_TODO, SOURCE_DIRS, GENERATED_DIRS)
        hits = [f for f in _tech_debt(findings) if f["tag"] == "FIXME"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 12)  # 第二个 hunk 从 +11 起，+1 行后命中

    def test_deleted_line_not_reported(self):
        # 技术债只扫新增行：删除行里的 TODO 不算新引入
        diff = ("diff --git a/kit/cli/lib/foo.py b/kit/cli/lib/foo.py\n"
                "--- a/kit/cli/lib/foo.py\n+++ b/kit/cli/lib/foo.py\n"
                "@@ -1,2 +1,2 @@\n"
                "-# TODO: old debt being removed\n"
                "+# debt removed, replacement line\n")
        findings = RC.scan(diff, SOURCE_DIRS, GENERATED_DIRS)
        self.assertEqual(_tech_debt(findings), [])

    def test_word_boundary_no_false_positive(self):
        diff = ("diff --git a/kit/cli/lib/foo.py b/kit/cli/lib/foo.py\n"
                "--- a/kit/cli/lib/foo.py\n+++ b/kit/cli/lib/foo.py\n"
                "@@ -1 +1,2 @@\n"
                "+a hacker and his todos and xxxlong strings\n")
        self.assertEqual(_tech_debt(RC.scan(diff, SOURCE_DIRS, GENERATED_DIRS)), [])


class OutOfBoundsTests(unittest.TestCase):
    def test_generated_dir_flagged_with_attribute(self):
        findings = RC.scan(GEN_FILE_DIFF, SOURCE_DIRS, GENERATED_DIRS)
        oob = _out_of_bounds(findings)
        self.assertEqual(len(oob), 1)
        self.assertEqual(oob[0]["path"], "kit/cli/__pycache__/foo.cpython-311.pyc")
        self.assertIn("generated_dirs", oob[0]["reason"])

    def test_outside_source_dirs_flagged_with_attribute(self):
        findings = RC.scan(OUTSIDE_SOURCE_DIFF, SOURCE_DIRS, GENERATED_DIRS)
        oob = _out_of_bounds(findings)
        self.assertEqual(len(oob), 1)
        self.assertEqual(oob[0]["path"], "other/random.py")
        self.assertIn("source_dirs", oob[0]["reason"])

    def test_rename_to_counted_as_changed(self):
        # 纯改名无 hunk，但目标落在 generated_dirs 也要告警
        diff = ("diff --git a/kit/cli/lib/a.py b/kit/cli/__pycache__/a.py\n"
                "similarity index 100%\n"
                "rename from kit/cli/lib/a.py\n"
                "rename to kit/cli/__pycache__/a.py\n")
        oob = _out_of_bounds(RC.scan(diff, SOURCE_DIRS, GENERATED_DIRS))
        self.assertEqual([f["path"] for f in oob], ["kit/cli/__pycache__/a.py"])
        self.assertIn("generated_dirs", oob[0]["reason"])

    def test_in_source_no_oob(self):
        findings = RC.scan(CLEAN_DIFF, SOURCE_DIRS, GENERATED_DIRS)
        self.assertEqual(_out_of_bounds(findings), [])


class RenderTests(unittest.TestCase):
    def test_clean_diff_renders_exactly_none(self):
        findings = RC.scan(CLEAN_DIFF, SOURCE_DIRS, GENERATED_DIRS)
        self.assertEqual(findings, [])
        self.assertEqual(RC.render(findings, "TASK-096", "HEAD~1", "HEAD"), "无")

    def test_empty_diff_renders_exactly_none(self):
        findings = RC.scan("", SOURCE_DIRS, GENERATED_DIRS)
        self.assertEqual(findings, [])
        self.assertEqual(RC.render(findings, "TASK-096", "HEAD~1", "HEAD"), "无")

    def test_render_contains_task_ref_and_finding(self):
        findings = RC.scan(DIFF_WITH_TODO, SOURCE_DIRS, GENERATED_DIRS)
        text = RC.render(findings, "TASK-096", "HEAD~1", "HEAD")
        self.assertIn("TASK-096", text)
        self.assertIn("kit/cli/lib/foo.py:2", text)
        self.assertIn("REFLECT", text)


if __name__ == "__main__":
    unittest.main()
