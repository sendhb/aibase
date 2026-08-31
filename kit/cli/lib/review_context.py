#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""review_context.py — Reviewer 最小上下文生成器（Token 优化）

用途：让 autoloop-reviewer 在独立会话中先获取一份紧凑的审查上下文，
而不是全仓扫描或一次性读取大量无关文件。

输出：
  1. TASK 关键元数据（status/priority/risk/assignee/rework-count）
  2. VERIFY 证据是否存在（pass/日期/文件名）
  3. Git 变更摘要（status + diff stat）

用法：
  python3 kit/cli/lib/review_context.py <task_id> [--max-stat-lines 100]

只读：不修改任何文件，不执行任何可能产生副作用的命令。
"""
import os
import re
import subprocess
import sys

TASK_RE = re.compile(r"^TASK-\d{3}-[a-z0-9-]+\.md$")
META_FIELD_RE = re.compile(r"^([a-z0-9-]+):\s*(.*)$")
_META_START_RE = re.compile(r"^metadata:?\s*$")


def find_project_root(start):
    d = os.path.dirname(os.path.abspath(start))
    while True:
        if os.path.isfile(os.path.join(d, "aios.config.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(start)))


def parse_metadata(content):
    """解析 frontmatter 的 metadata 块，返回 {key: value}。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    in_metadata = False
    meta = {}
    for line in lines[1:]:
        s = line.strip()
        if s == "---":
            break
        if _META_START_RE.match(s):
            in_metadata = True
            continue
        if not in_metadata:
            continue
        m = META_FIELD_RE.match(s)
        if m:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def find_task(root, task_id):
    tasks_dir = os.path.join(root, "runtime", "tasks")
    if not os.path.isdir(tasks_dir):
        return None
    short = task_id.upper()
    for f in sorted(os.listdir(tasks_dir)):
        if not TASK_RE.match(f):
            continue
        if f.startswith(short + "-") or f == short + ".md":
            return os.path.join(tasks_dir, f)
    return None


def verify_records(root, task_id):
    verify_dir = os.path.join(root, "runtime", "verification")
    if not os.path.isdir(verify_dir):
        return []
    task_norm = task_id.upper().replace("-", "")
    records = []
    for f in sorted(os.listdir(verify_dir)):
        if not f.startswith("VERIFY-") or not f.endswith(".md"):
            continue
        low = f.lower()
        low_norm = low.replace("-", "")
        if task_norm.lower() in low_norm or task_id.upper() in low:
            path = os.path.join(verify_dir, f)
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
                meta = parse_metadata(content)
                records.append({
                    "file": f,
                    "result": meta.get("result", "?"),
                    "date": meta.get("date", "?"),
                })
            except OSError:
                records.append({"file": f, "result": "?", "date": "?"})
    return records


def run_git(root, args):
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=root, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=10,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def main(argv):
    if not argv:
        print("用法: python3 kit/cli/lib/review_context.py <task_id> [--max-stat-lines N]", file=sys.stderr)
        return 2
    task_id = argv[0]
    max_stat_lines = 100
    if "--max-stat-lines" in argv:
        idx = argv.index("--max-stat-lines")
        try:
            max_stat_lines = max(1, int(argv[idx + 1]))
        except (IndexError, ValueError):
            pass

    root = find_project_root(__file__)
    task_path = find_task(root, task_id)
    if not task_path:
        print(f"✗ 未找到任务: {task_id}（runtime/tasks/ 下不存在）", file=sys.stderr)
        return 1

    with open(task_path, encoding="utf-8") as f:
        meta = parse_metadata(f.read())

    print(f"# Review Context — {task_id}")
    print(f"task_file: {os.path.relpath(task_path, root)}")
    for key in ("status", "priority", "risk", "assignee", "reviewer", "rework-count"):
        print(f"{key}: {meta.get(key, '')}")

    records = verify_records(root, task_id)
    print(f"verify_records: {len(records)}")
    for r in records:
        print(f"  - {r['file']} result={r['result']} date={r['date']}")

    status = run_git(root, ["status", "--short"])
    if status:
        print("\n# Git status")
        print(status)

    diff_stat = run_git(root, ["diff", "--stat", "HEAD"])
    if diff_stat:
        lines = diff_stat.splitlines()
        print("\n# Git diff stat (HEAD)")
        print("\n".join(lines[:max_stat_lines]))
        if len(lines) > max_stat_lines:
            print(f"... (truncated {len(lines) - max_stat_lines} lines)")

    cached_stat = run_git(root, ["diff", "--cached", "--stat"])
    if cached_stat:
        lines = cached_stat.splitlines()
        print("\n# Git diff stat (staged)")
        print("\n".join(lines[:max_stat_lines]))
        if len(lines) > max_stat_lines:
            print(f"... (truncated {len(lines) - max_stat_lines} lines)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
