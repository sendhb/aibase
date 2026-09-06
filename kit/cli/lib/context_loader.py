#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""context_loader.py — 上下文装配器：按 task 推导最小文件集（TASK-094）

把上下文纪律从"军规"（boot 文档纯规劝）变"军械"（机械执行）：
  collect   收集最小文件集（TASK 本体 + depends-on 链 + checkpoint + 模块文档
            + TASK 提及源文件 + 当前 diff 相关文件），每项含路径/行数/理由
  filter    机械排除 generated_dirs / 二进制 / 隐藏 / 不可读文件
  rank      TASK 本体 > depends-on > checkpoint > 模块文档 > 源文件；
            例外（TASK-095）：in-progress 任务 checkpoint 升至最前
            （PRI_CHECKPOINT_TOP，断点恢复优先装载，与本体同为不截断保护）
  allocate  总行预算内按优先级截断；TASK 本体受保护永不截断；
            被截断文件显式标注 `[TRUNCATED 到第 N 行]`

预算推导（AGENTS.md 原则）：首次进入读 3 个入口文件 × 单文件 ≤200 行
→ 默认 600 行；CLI `--budget N` 可调。

用途：
  python3 kit/cli/context TASK-xxx [--budget N]      # 薄入口 kit/cli/context
  autoloop coder/reviewer 在 prompt 组装前注入装配清单（fail-open）

只读：不修改任何文件。纯 stdlib（复用 config.py / review_context.py）。
"""
import os
import re
import sys

_LIB = os.path.dirname(os.path.abspath(__file__))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import config  # noqa: E402
import review_context  # noqa: E402

# ranking 优先级（数值大者先装配；TASK-094 计划第 3 条四级保序）
PRI_TASK = 100        # TASK 本体（受保护：永不截断）
PRI_DEP = 95          # depends-on 链 TASK
PRI_CHECKPOINT = 90       # checkpoint 常规档（runtime/states/ 或 runtime/checkpoints/）
PRI_CHECKPOINT_TOP = 105  # in-progress 任务 checkpoint：断点恢复优先装载（TASK-095）
PRI_MODULE = 70       # knowledge/modules/ 相关模块文档
PRI_SOURCE = 50       # TASK 提及 / 当前 diff 相关源文件

DEFAULT_BUDGET = 600  # AGENTS.md：首次 3 个入口 × 单文件 ≤200 行
MENTION_CAP = 30      # 提及/diff 文件收集上限（防清单爆炸）
MODULE_CAP = 10

BINARY_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
               ".zip", ".tar", ".gz", ".tgz", ".7z", ".so", ".dll", ".exe",
               ".pyc", ".pyo", ".class", ".woff", ".woff2", ".ttf",
               ".db", ".sqlite", ".mp3", ".mp4"}

_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.\-]+/)+[A-Za-z0-9_.\-]+")
_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def line_count(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _under(rel, prefixes):
    return any(rel == p.rstrip("/") or rel.startswith(p.rstrip("/") + "/")
               for p in prefixes)


class ContextPlan:
    """装配结果：items（预算分配后）、filtered（被 Filter 排除的 (路径, 原因)）。"""

    def __init__(self, task_id, root, budget, items, filtered):
        self.task_id = task_id
        self.root = root
        self.budget = budget
        self.items = items
        self.filtered = filtered

    @property
    def allocated(self):
        return sum(it["alloc"] for it in self.items)

    def render(self):
        w_path = max([len(it["path"]) for it in self.items] + [4])
        w_lines = max([len(str(it["alloc"])) for it in self.items] + [5])
        head = "# Context Plan — %s（budget %d 行，装配 %d 行 / %.1f%%）" % (
            self.task_id, self.budget, self.allocated,
            100.0 * self.allocated / self.budget)
        rows = ["%-*s  %*s  %8s  %s" % (w_path, "path", w_lines, "lines",
                                        "budget%", "note")]
        for it in self.items:
            note = it["reason"]
            if it["truncated"]:
                note = "[TRUNCATED 到第 %d 行] %s" % (it["alloc"], it["reason"])
            rows.append("%-*s  %*d  %7.1f%%  %s" % (
                w_path, it["path"], w_lines, it["alloc"],
                100.0 * it["alloc"] / self.budget, note))
        if self.filtered:
            rows.append("filtered（generated_dirs/二进制/不可读）: %d 个文件已排除"
                        % len(self.filtered))
        return head + "\n" + "\n".join(rows)


def collect(root, task_id):
    """收集最小文件集（未做预算分配）。返回 (items, filtered)；任务缺失返回 ([], [])。"""
    items, filtered, seen = [], [], set()
    task_path = review_context.find_task(root, task_id)
    if not task_path:
        return items, filtered

    try:
        cfg = config.load_config(root)
        src, gen = cfg.get("source_dirs") or [], cfg.get("generated_dirs") or []
    except config.ConfigError:
        src, gen = [], []

    def add(path, pri, reason):
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        if rel in seen:
            return
        ext = os.path.splitext(rel)[1].lower()
        name = rel.rsplit("/", 1)[-1]
        if ext in BINARY_EXTS or name.startswith("."):
            filtered.append((rel, "binary/hidden"))
            return
        if _under(rel, gen):
            filtered.append((rel, "generated_dirs"))
            return
        lines = line_count(path)
        if lines <= 0:
            filtered.append((rel, "unreadable"))
            return
        seen.add(rel)
        items.append({"path": rel, "lines": lines, "alloc": lines,
                      "reason": reason, "pri": pri, "truncated": False})

    with open(task_path, encoding="utf-8") as f:
        task_text = f.read()
    meta = review_context.parse_metadata(task_text)
    add(task_path, PRI_TASK, "task-body")

    # depends-on 链（防环；与本体同属 TASK 档，仅次之）
    dep_ids = re.findall(r"TASK-\d{3}", meta.get("depends-on", "") or "")
    for dep in dict.fromkeys(dep_ids):
        if dep == task_id.upper():
            continue
        dep_path = review_context.find_task(root, dep)
        if dep_path:
            add(dep_path, PRI_DEP, "depends-on")

    # checkpoint（TASK-095 断点恢复）：
    #   主入口 runtime/states/STATE-PROGRESS-<task>.md（autoloop-coder 会话收尾
    #   钩子落盘）；runtime/checkpoints/ 为 TASK-094 预留兼容位，继续扫描。
    #   in-progress 任务把 checkpoint 升至清单最前（断点恢复优先装载），
    #   reason 带「断点恢复」标注；非 in-progress 保持常规档次序。
    in_progress = (meta.get("status") or "").strip().lower() == "in-progress"
    ck_pri = PRI_CHECKPOINT_TOP if in_progress else PRI_CHECKPOINT
    ck_reason = ("checkpoint（断点恢复优先装载）" if in_progress else "checkpoint")
    sp = os.path.join(root, "runtime", "states",
                      "STATE-PROGRESS-%s.md" % task_id.upper())
    if os.path.isfile(sp):
        add(sp, ck_pri, ck_reason)
    ck_dir = os.path.join(root, "runtime", "checkpoints")
    if os.path.isdir(ck_dir):
        norm = task_id.upper().replace("-", "")
        for f in sorted(os.listdir(ck_dir)):
            p = os.path.join(ck_dir, f)
            if norm in f.upper().replace("-", "") and os.path.isfile(p):
                add(p, ck_pri, ck_reason)

    # knowledge/modules/ 相关模块文档（task tags/标题词与文档内容取交集，best-effort）
    km_dir = os.path.join(root, "knowledge", "modules")
    if os.path.isdir(km_dir):
        words = set(_WORD_RE.findall((meta.get("tags", "") or "").lower()))
        words |= set(_WORD_RE.findall(
            os.path.basename(task_path).lower().replace(".md", "")))
        hits = []
        for dirpath, _, files in os.walk(km_dir):
            for f in sorted(files):
                if f.endswith(".md"):
                    hits.append(os.path.join(dirpath, f))
        for p in hits:
            if len([i for i in items if i["pri"] == PRI_MODULE]) >= MODULE_CAP:
                break
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4096).lower()
            except OSError:
                continue
            if words & set(_WORD_RE.findall(head)):
                add(p, PRI_MODULE, "module-doc")

    # TASK 提及的源文件（限 source_dirs 内真实存在的文件）
    mentions = list(dict.fromkeys(_PATH_RE.findall(task_text)))[:MENTION_CAP]
    for tok in mentions:
        tok = tok.rstrip(".,;:)")
        p = os.path.join(root, tok)
        # gen 目录的候选也走 add()，让 Filter 记录排除原因（而非静默跳过）
        if os.path.isfile(p) and (_under(tok, src) or _under(tok, gen)):
            add(p, PRI_SOURCE, "task-mention")

    # 当前 diff 相关文件（git status + diff，限 source_dirs；同档内先于提及）
    def git_paths(args, skip):
        out = review_context.run_git(root, args)
        return [ln[skip:].strip().strip('"') for ln in out.splitlines()
                if len(ln) > skip and ln[skip:].strip()]

    diff_paths = git_paths(["status", "--porcelain"], 3) + \
        git_paths(["diff", "--name-only", "HEAD"], 0)
    for rel in list(dict.fromkeys(diff_paths))[:MENTION_CAP]:
        p = os.path.join(root, rel)
        if os.path.isfile(p) and (_under(rel, src) or _under(rel, gen)):
            add(p, PRI_SOURCE, "diff")

    return items, filtered


def allocate(items, budget):
    """按优先级降序（稳定）装配预算：TASK 本体与 in-progress checkpoint
    （PRI_CHECKPOINT_TOP，TASK-095 断点恢复）受保护永不截断，其余顺序分配。"""
    items.sort(key=lambda it: -it["pri"])
    remaining = budget
    for it in items:
        if it["pri"] >= PRI_TASK:  # ranking 保序：TASK 本体不被截断
            remaining -= it["lines"]
            continue
        it["alloc"] = min(it["lines"], max(0, remaining))
        remaining -= it["alloc"]
        it["truncated"] = it["alloc"] < it["lines"]
    return items


def assemble(task_id, root=None, budget=None):
    """assemble(task_id) → ContextPlan；任务不存在返回 None。只读。"""
    root = root or review_context.find_project_root(__file__)
    budget = DEFAULT_BUDGET if budget is None else max(1, int(budget))
    items, filtered = collect(root, task_id)
    if not items:
        return None
    allocate(items, budget)
    return ContextPlan(task_id, root, budget, items, filtered)


if __name__ == "__main__":
    print("用法: python3 kit/cli/context <TASK-ID> [--budget N]", file=sys.stderr)
    sys.exit(2)
