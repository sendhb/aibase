"""TASK-095 — 会话断点恢复单测（checkpoint 自动落盘 + 装载恢复优先）。

覆盖：
- run_once 收尾钩子：in-progress 任务模拟 coder 轮次结束，ok / timeout 124 /
  error 三条路径都生成 runtime/states/STATE-PROGRESS-<task>.md，含
  "已完成/下一步"段与 last-exit 标签、round 计数（验收标准 1）
- checkpoint 内容机械提取：当前进度表 ✅/⏳/❌ + 验收标准勾选态
- 写盘失败不阻塞 autoloop 主流程（stderr 告警，run_once 照常返回，验收标准 4）
- no_task 空转轮不产生 checkpoint
- context loader：in-progress 任务 checkpoint 排装配清单首位且带"断点恢复"
  标注（验收标准 2）；非 in-progress 保持 TASK-094 次序；预算内不截断；
  runtime/checkpoints/ 兼容位仍收集

安全边界：不调用 main()（find_project_root 恒定位真实仓库）；LLM 一律 stub
（run_once 的 llm_fn 注入）；全部用临时项目根，不触碰真实仓库状态。
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_KIT = os.path.dirname(_HERE)
_LIB = os.path.join(_KIT, "cli", "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import autoloop_coder as C  # noqa: E402
import context_loader as CL  # noqa: E402
import llm  # noqa: E402

TASK_TMPL = """---
name: %(tid)s-drill
description: 断点恢复单测夹具
metadata:
  type: task
  status: %(status)s
  created: 2026-09-04
  priority: P2
  risk: P2
  approval-ref: none
  assignee: any
  reviewer: any
  depends-on: []
  rework-count: 0
  tags: [drill]
---

# %(tid)s — drill

涉及文件：src/main.py

## 验收标准

- [x] 已完成项（勾选）
- [ ] 待办项（未勾选）

## 当前进度

| 项 | 状态 | 位置 |
|----|------|------|
| 钩子 | ✅ | done |
| 装载 | ⏳ | — |
| 演练 | ❌ | blocked |
"""
CONFIG_YAML = """version: 1
profile: test
source_dirs:
  - src/
  - runtime/
generated_dirs: []
commands:
  build: "true"
  lint: "true"
  test: "true"
  check: "true"
"""


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def make_project(tmp, tid="TASK-900", status="in-progress"):
    """构造临时项目：返回 root。任务本体含 ✅/⏳/❌ 进度行 + 勾选态验收标准。"""
    root = tempfile.mkdtemp(dir=tmp)
    write(os.path.join(root, "aios.config.yaml"), CONFIG_YAML)
    write(os.path.join(root, "runtime", "tasks", "%s-drill.md" % tid),
          TASK_TMPL % {"tid": tid, "status": status})
    write(os.path.join(root, "src", "main.py"),
          "\n".join("x%d = %d" % (i, i) for i in range(60)) + "\n")
    return root


def make_task_log(root, tid, rounds=1, extra_lines=("step 1", "step 2")):
    """写 per-task log：rounds 个轮次头 + 附加输出行（round 计数/尾部提取来源）。"""
    lines = []
    for n in range(1, rounds + 1):
        lines.append("=== [2026-09-04T10:00:0%d] coder round %d | provider pi "
                     "| PID %d ===" % (n, n, 100 + n))
        lines.extend(extra_lines)
    write(os.path.join(root, "runtime", "logs", "tasks", "%s.log" % tid),
          "\n".join(lines) + "\n")


def run_round(root, rc):
    """run_once 单轮（stub LLM 返回 rc，stub task CLI 记录调用）；返回 (rc, calls)。"""
    calls = []

    def task_cli_stub(r, *args):
        calls.append(args)
        return 0

    def llm_stub(provider, prompt, **kw):
        return rc

    opts = C.parse_args(["--once"])
    rc = C.run_once(root, opts, None, os.path.join(root, "runtime", "logs"),
                    task_cli_fn=task_cli_stub, llm_fn=llm_stub)
    return rc, calls


class CheckpointWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ck(self, root, tid="TASK-900"):
        return os.path.join(root, "runtime", "states",
                            "STATE-PROGRESS-%s.md" % tid)

    # 验收标准 1：in-progress 任务 + timeout 124 场景，checkpoint 生成且含两段
    def test_timeout_124_writes_checkpoint_with_sections(self):
        root = make_project(self.tmp, status="in-progress")
        make_task_log(root, "TASK-900", rounds=1)
        rc, _ = run_round(root, llm.TIMEOUT_EXIT)
        self.assertEqual(rc, llm.TIMEOUT_EXIT)
        path = self.ck(root)
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("## 已完成", text)
        self.assertIn("## 下一步", text)
        self.assertIn("## 阻塞", text)
        self.assertIn("last-exit: timeout(%d)" % llm.TIMEOUT_EXIT, text)
        self.assertIn("round: 1", text)

    # ok / error 路径同样落盘（三条路径全覆盖），round 计数取自轮次头
    def test_ok_and_error_paths_write_checkpoint(self):
        root = make_project(self.tmp, status="in-progress")
        make_task_log(root, "TASK-900", rounds=3)
        rc, _ = run_round(root, 0)
        self.assertEqual(rc, 0)
        with open(self.ck(root), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("last-exit: ok", text)
        self.assertIn("round: 3", text)

        root2 = make_project(self.tmp, tid="TASK-901", status="in-progress")
        make_task_log(root2, "TASK-901", rounds=2)
        rc, _ = run_round(root2, 7)
        self.assertEqual(rc, 7)
        with open(self.ck(root2, "TASK-901"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("last-exit: error(7)", text)
        self.assertIn("上一轮会话异常退出: error(7)", text)

    # 内容机械提取：进度表 ✅/⏳/❌ 与验收勾选态分别进 已完成/下一步/阻塞
    def test_progress_extraction(self):
        root = make_project(self.tmp, status="in-progress")
        make_task_log(root, "TASK-900")
        run_round(root, 0)
        with open(self.ck(root), encoding="utf-8") as f:
            text = f.read()
        done = text.split("## 已完成", 1)[1].split("## 下一步", 1)[0]
        todo = text.split("## 下一步", 1)[1].split("## 阻塞", 1)[0]
        blocked = text.split("## 阻塞", 1)[1].split("## 会话尾部", 1)[0]
        self.assertIn("- 钩子（当前进度表）", done)
        self.assertIn("- 已完成项（勾选）", done)
        self.assertIn("- 装载（当前进度表）", todo)
        self.assertIn("- 待办项（未勾选）", todo)
        self.assertNotIn("待办项", done)
        self.assertIn("- 演练（当前进度表 ❌）", blocked)

    # 会话尾部随 checkpoint 落盘（超时/异常定位用）
    def test_session_tail_included(self):
        root = make_project(self.tmp, status="in-progress")
        make_task_log(root, "TASK-900", rounds=1,
                      extra_lines=["panic: mid-step", "partial edit done"])
        run_round(root, llm.TIMEOUT_EXIT)
        with open(self.ck(root), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("panic: mid-step", text)
        self.assertIn("partial edit done", text)

    # 验收标准 4：写盘失败不阻塞主流程（states 为文件 → 打开必败），stderr 告警
    def test_write_failure_does_not_block(self):
        root = make_project(self.tmp, status="in-progress")
        write(os.path.join(root, "runtime", "states"), "not a dir\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, _ = run_round(root, 0)
        self.assertEqual(rc, 0)  # 主流程照常返回
        self.assertIn("checkpoint 写盘失败", err.getvalue())

    # no_task 空转轮不产生 checkpoint（无可断点之处）
    def test_no_task_round_no_checkpoint(self):
        root = tempfile.mkdtemp(dir=self.tmp)
        write(os.path.join(root, "aios.config.yaml"), CONFIG_YAML)
        os.makedirs(os.path.join(root, "runtime", "tasks"))
        rc, _ = run_round(root, 0)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(root, "runtime", "states")))

    # 纯函数：checkpoint 路径 = task ID 的纯函数
    def test_checkpoint_relpath_pure(self):
        self.assertEqual(C.checkpoint_relpath("TASK-095"),
                         "runtime/states/STATE-PROGRESS-TASK-095.md")


class CheckpointLoadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ck(self, root, tid):
        return os.path.join(root, "runtime", "states",
                            "STATE-PROGRESS-%s.md" % tid)

    # 验收标准 2：in-progress 任务 checkpoint 排首位且带"断点恢复"标注
    def test_inprogress_checkpoint_first_with_marker(self):
        root = make_project(self.tmp, tid="TASK-901", status="in-progress")
        write(self.ck(root, "TASK-901"),
              "# STATE-PROGRESS — TASK-901\n\n## 已完成\n- 钩子\n")
        plan = CL.assemble("TASK-901", root=root)
        first = plan.items[0]
        self.assertEqual(first["path"],
                         "runtime/states/STATE-PROGRESS-TASK-901.md")
        self.assertEqual(first["pri"], CL.PRI_CHECKPOINT_TOP)
        self.assertIn("断点恢复", first["reason"])
        text = plan.render()
        self.assertIn("断点恢复", text.splitlines()[2])  # 首条数据行

    # 非 in-progress：保持 TASK-094 次序（本体 > checkpoint），reason 无标注
    def test_non_inprogress_checkpoint_after_task_body(self):
        root = make_project(self.tmp, tid="TASK-902", status="open")
        write(self.ck(root, "TASK-902"), "# STATE-PROGRESS — TASK-902\n")
        plan = CL.assemble("TASK-902", root=root)
        self.assertEqual(plan.items[0]["pri"], CL.PRI_TASK)
        ck = [it for it in plan.items
              if it["path"].endswith("STATE-PROGRESS-TASK-902.md")]
        self.assertEqual(len(ck), 1)
        self.assertEqual(ck[0]["pri"], CL.PRI_CHECKPOINT)
        self.assertEqual(ck[0]["reason"], "checkpoint")

    # 预算紧张时 in-progress checkpoint 不被截断（与 TASK 本体同为保护档）
    def test_inprogress_checkpoint_not_truncated(self):
        root = make_project(self.tmp, tid="TASK-903", status="in-progress")
        ck_path = self.ck(root, "TASK-903")
        write(ck_path, "\n".join("checkpoint line %d" % i for i in range(20))
              + "\n")
        plan = CL.assemble("TASK-903", root=root, budget=50)
        ck = [it for it in plan.items if it["path"].endswith(".md")
              and "STATE-PROGRESS" in it["path"]][0]
        self.assertEqual(ck["alloc"], 20)  # 20 行内容（line_count 口径）
        self.assertFalse(ck["truncated"])
        src = [it for it in plan.items if it["path"] == "src/main.py"][0]
        self.assertTrue(src["truncated"])

    # runtime/checkpoints/ 兼容位（TASK-094 预留）仍被收集
    def test_checkpoints_dir_compat_collected(self):
        root = make_project(self.tmp, tid="TASK-904", status="open")
        write(os.path.join(root, "runtime", "checkpoints",
                           "probe-TASK-904-x.md"), "# legacy checkpoint\n")
        plan = CL.assemble("TASK-904", root=root)
        ck = [it for it in plan.items if "checkpoints/" in it["path"]]
        self.assertEqual(len(ck), 1)
        self.assertEqual(ck[0]["pri"], CL.PRI_CHECKPOINT)


if __name__ == "__main__":
    unittest.main()
