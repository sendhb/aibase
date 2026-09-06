"""TASK-094 — context_loader 单测（装配 / Filter / 预算截断 / ranking / 依赖链）。

场景全部用临时项目根构造（aios.config.yaml + runtime/tasks + src/gen/knowledge），
不触碰真实仓库状态；另含一个真实仓库的 TASK-093 冒烟断言（验收标准 1）。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import context_loader as CL  # noqa: E402

TASK_TMPL = """---
name: %(tid)s-demo
description: demo
metadata:
  type: task
  status: open
  created: 2026-09-04
  priority: P2
  risk: P2
  approval-ref: none
  assignee: any
  reviewer: any
  depends-on: %(deps)s
  rework-count: 0
  tags: [%(tags)s]
---

# %(tid)s — demo

涉及文件：%(mentions)s
"""
CONFIG_YAML = """version: 1
profile: test
source_dirs:
  - src/
  - runtime/
  - knowledge/
generated_dirs:
  - gen/
commands:
  build: "true"
  lint: "true"
  test: "true"
  check: "true"
"""


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, bytes) else "w"
    with open(path, mode, encoding=None if mode == "wb" else "utf-8") as f:
        f.write(content)


def make_project(tmp, task_lines=10, main_lines=60, other_lines=60,
                 deps="[]", tags="demo alpha", mentions="src/main.py src/other.py",
                 module_doc=True):
    """构造临时项目：返回 root。src/main.py、src/other.py、gen/thing.py、模块文档。"""
    root = tempfile.mkdtemp(dir=tmp)
    write(os.path.join(root, "aios.config.yaml"), CONFIG_YAML)
    write(os.path.join(root, "runtime", "tasks", "TASK-001-demo.md"),
          TASK_TMPL % {"tid": "TASK-001", "deps": deps, "tags": tags,
                       "mentions": mentions})
    write(os.path.join(root, "src", "main.py"),
          "\n".join("x%d = %d" % (i, i) for i in range(max(1, main_lines))) + "\n")
    write(os.path.join(root, "src", "other.py"),
          "\n".join("y%d = %d" % (i, i) for i in range(max(1, other_lines))) + "\n")
    write(os.path.join(root, "gen", "thing.py"), "generated = 1\n")
    if module_doc:
        write(os.path.join(root, "knowledge", "modules", "demo.md"),
              "# demo module\nalpha 相关模块文档。\n")
    return root


def rel_paths(plan):
    return [it["path"] for it in plan.items]


class ContextLoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Filter 机械生效：generated_dirs / 二进制不进装配清单（验收标准 5）
    def test_filter_excludes_generated_dirs_and_binary(self):
        root = make_project(self.tmp, mentions="src/main.py gen/thing.py src/blob.png")
        write(os.path.join(root, "src", "blob.png"), b"\x89PNG\r\n\x1a\n")
        plan = CL.assemble("TASK-001", root=root)
        paths = rel_paths(plan)
        self.assertNotIn("gen/thing.py", paths)
        self.assertNotIn("src/blob.png", paths)
        filtered = dict(plan.filtered)
        self.assertEqual(filtered.get("gen/thing.py"), "generated_dirs")
        self.assertEqual(filtered.get("src/blob.png"), "binary/hidden")

    # 预算 50 超预算场景：TASK 本体不被截断，其余按优先级截断并标注行号（验收标准 2）
    def test_budget_truncation_task_body_protected(self):
        root = make_project(self.tmp, module_doc=False)
        task_file = os.path.join(root, "runtime", "tasks", "TASK-001-demo.md")
        task_lines = CL.line_count(task_file)
        self.assertLess(task_lines, 50)  # 本体在预算内，剩余预算分配给源文件
        plan = CL.assemble("TASK-001", root=root, budget=50)
        task_item = plan.items[0]
        self.assertTrue(task_item["path"].startswith("runtime/tasks/TASK-001"))
        self.assertFalse(task_item["truncated"])
        self.assertEqual(task_item["alloc"], task_lines)  # 本体整文件保留
        others = plan.items[1:]
        self.assertTrue(all(it["truncated"] for it in others))
        text = plan.render()
        # main.py 分到剩余预算：显式标注截断行号
        self.assertIn("[TRUNCATED 到第 %d 行]" % (50 - task_lines), text)
        self.assertIn("[TRUNCATED 到第 0 行]", text)

    # ranking 保序：TASK 本体 > 模块文档 > 源文件
    def test_ranking_order(self):
        root = make_project(self.tmp)
        plan = CL.assemble("TASK-001", root=root, budget=1000)
        paths = rel_paths(plan)
        self.assertTrue(paths[0].startswith("runtime/tasks/TASK-001"))
        self.assertLess(paths.index("knowledge/modules/demo.md"),
                        paths.index("src/main.py"))

    # 三列输出：路径 / 行数 / 预算占比（验收标准 1 的列结构）
    def test_render_three_columns(self):
        root = make_project(self.tmp)
        plan = CL.assemble("TASK-001", root=root)
        text = plan.render()
        header = text.splitlines()[1]
        self.assertIn("path", header)
        self.assertIn("lines", header)
        self.assertIn("budget%", header)
        for it in plan.items:
            self.assertIn("%.1f%%" % (100.0 * it["alloc"] / plan.budget), text)

    # depends-on 链收集
    def test_depends_on_chain_collected(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        write(os.path.join(root, "aios.config.yaml"), CONFIG_YAML)
        write(os.path.join(root, "runtime", "tasks", "TASK-001-demo.md"),
              TASK_TMPL % {"tid": "TASK-001", "deps": "[TASK-002]", "tags": "demo",
                           "mentions": ""})
        write(os.path.join(root, "runtime", "tasks", "TASK-002-base.md"),
              TASK_TMPL % {"tid": "TASK-002", "deps": "[]", "tags": "base",
                           "mentions": ""})
        plan = CL.assemble("TASK-001", root=root)
        paths = rel_paths(plan)
        self.assertTrue(any(p.endswith("TASK-002-base.md") for p in paths))
        dep = [it for it in plan.items if it["path"].endswith("TASK-002-base.md")][0]
        self.assertEqual(dep["reason"], "depends-on")
        self.assertEqual(plan.items[0]["pri"], CL.PRI_TASK)
        self.assertEqual(dep["pri"], CL.PRI_DEP)

    # 任务缺失 → None（CLI 退出码 1）
    def test_missing_task_returns_none(self):
        root = make_project(self.tmp)
        self.assertIsNone(CL.assemble("TASK-099", root=root))

    # 真实仓库冒烟（验收标准 1）：TASK-093 本体在列
    def test_real_repo_task093(self):
        plan = CL.assemble("TASK-093", root=os.path.dirname(_KIT))
        self.assertIsNotNone(plan)
        self.assertTrue(rel_paths(plan)[0].startswith("runtime/tasks/TASK-093"))

    # autoloop prompt 注入（验收标准 3 的行为面）：注入后含装配清单，缺省不含
    def test_autoloop_prompt_assembly_injection(self):
        import autoloop_coder
        import autoloop_reviewer
        base = autoloop_coder.build_prompt("TASK-901", "TASK-901-x.md", True, "done")
        with_asm = autoloop_coder.build_prompt(
            "TASK-901", "TASK-901-x.md", True, "done", assembly="kit/cli/task  0  0.0%")
        self.assertNotIn("装配清单", base)
        self.assertIn("装配清单", with_asm)
        # 验收标准 3：boot 接入点存在（import context_loader）
        with open(autoloop_coder.__file__, encoding="utf-8") as f:
            self.assertIn("import context_loader", f.read())
        rbase = autoloop_reviewer.build_prompt("TASK-901", "TASK-901-x.md", "rev", "三问（P2）")
        rwith = autoloop_reviewer.build_prompt(
            "TASK-901", "TASK-901-x.md", "rev", "三问（P2）", assembly="x 1 0.2%")
        self.assertNotIn("装配清单", rbase)
        self.assertIn("装配清单", rwith)

    # git diff 文件收集（有 git 仓库时）——初始化临时仓库并提交后修改文件
    def test_diff_files_collected(self):
        root = make_project(self.tmp, mentions="")
        try:
            def git(*args):
                subprocess.run(["git"] + list(args), cwd=root, check=True,
                               capture_output=True)
            git("init", "-q")
            git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
            git("-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", "init")
            write(os.path.join(root, "src", "main.py"), "changed = 1\n")
            plan = CL.assemble("TASK-001", root=root)
            self.assertIn("src/main.py", rel_paths(plan))
            self.assertIn("diff",
                          [it["reason"] for it in plan.items
                           if it["path"] == "src/main.py"])
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git 不可用")


if __name__ == "__main__":
    unittest.main()
