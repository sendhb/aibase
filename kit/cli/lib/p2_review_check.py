#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""p2_review_check.py — P2 Review 半自动检查（Token 优化）

用途：在 Reviewer 独立会话前自动完成 P2 三问中可脚本化的部分：

1. verify 是否真实通过（VERIFY 记录存在且 result=pass）
2. 是否改动了 generated_dirs（治理红线）
3. 改动文件清单 / 是否明显超出 TASK 声明范围（启发式）

输出为紧凑文本，Reviewer 只需要基于输出做“验收标准是否满足”的判断。

用法：
  python3 kit/cli/lib/p2_review_check.py <task_id>
"""
import os
import re
import sys

# 复用 review_context 的只读工具（同一目录）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg_lib  # noqa: E402
from review_context import find_project_root, find_task, run_git, verify_records  # noqa: E402

_SCOPE_RE = re.compile(r"^##\s+范围\s*$")
_HEADING_RE = re.compile(r"^##\s+")


def changed_files(root):
    files = set()
    for out in (
        run_git(root, ["diff", "--name-only", "HEAD"]),
        run_git(root, ["diff", "--cached", "--name-only"]),
        run_git(root, ["ls-files", "--others", "--exclude-standard"]),
    ):
        for line in out.splitlines():
            line = line.strip()
            if line:
                files.add(line)
    return sorted(files)


def task_scope_items(task_path):
    """从 TASK 正文的 ## 范围 节提取路径/模块列表（启发式）。"""
    try:
        with open(task_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    lines = text.splitlines()
    in_scope = False
    items = []
    for line in lines:
        if _SCOPE_RE.match(line.strip()):
            in_scope = True
            continue
        if in_scope and _HEADING_RE.match(line.strip()):
            break
        if in_scope:
            s = line.strip()
            if s.startswith("- "):
                items.append(s[2:].strip())
    return items


def main(argv):
    if not argv:
        print("用法: python3 kit/cli/lib/p2_review_check.py <task_id>", file=sys.stderr)
        return 2
    task_id = argv[0]
    root = find_project_root(__file__)
    task_path = find_task(root, task_id)
    if not task_path:
        print(f"✗ 未找到任务: {task_id}", file=sys.stderr)
        return 1

    records = verify_records(root, task_id)
    verify_pass = any(r.get("result") == "pass" for r in records)

    try:
        cfg = cfg_lib.load_config(root)
        gen_dirs = [d.rstrip("/") for d in cfg.get("generated_dirs", [])]
    except cfg_lib.ConfigError:
        gen_dirs = []

    files = changed_files(root)
    gen_violations = [
        f for f in files
        if any(f == d or f.startswith(d + "/") for d in gen_dirs)
    ]

    scope_items = task_scope_items(task_path)
    scope_paths = [s for s in scope_items if "/" in s or "." in s]
    scope_violations = []
    if scope_paths:
        for f in files:
            if not any(f == p or f.startswith(p.rstrip("/") + "/") or p in f for p in scope_paths):
                scope_violations.append(f)

    print(f"# P2 Review Check — {task_id}")
    print(f"verify: {'PASS' if verify_pass else 'FAIL'} ({len(records)} record(s))")
    print(f"generated_dirs: {'FAIL' if gen_violations else 'PASS'} ({len(gen_violations)} violation(s))")
    print(f"changed_files: {len(files)}")
    for f in files:
        print(f"  - {f}")
    if scope_paths:
        if scope_violations:
            print(f"scope: WARNING ({len(scope_violations)} file(s) outside declared paths)")
            for f in scope_violations:
                print(f"  - {f}")
        else:
            print("scope: OK")
    elif scope_items:
        print("scope: MANUAL (declared items are not explicit paths)")
    else:
        print("scope: UNKNOWN (no explicit scope list)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
