"""reflect_check.py — Reflect 机械化核心（TASK-096）：diff 扫描（技术债标记 + 越界文件）。

职责边界（engine.md §4"机械项"）：
- 机械扫描 git diff 新增行中的技术债标记（TODO/FIXME/HACK/XXX，逐文件逐行号）；
- 越界检查：改动文件 ∉ source_dirs 或 ∈ generated_dirs → 越界告警（指明目录属性）；
- 产出是**建议**（REFLECT 记录段，贴 TASK 备注），不进状态机否决链——
  机械否决权只在 verify（build/lint/test/check）与治理闸门（P0/rework）。
- 判断性自省（架构违规/测试覆盖/规格对齐）不在本模块职责内，仍由模型完成。

纯 stdlib、零外部依赖（与 lib/config.py 一致）；diff 以文本替身注入，
不依赖真实 git 状态（单测可构造替身，TASK-096 验收标准）。

diff 解析覆盖面（够用即可，刻意保持薄）：
- 以 `+++ b/<path>` 定位改动文件（/dev/null = 被删除侧，跳过）；
- `rename to <path>` 也计入改动文件（纯改名无 hunk，避免漏报越界）；
- 二进制文件（`Binary files ... differ`，无 +++ 行）不扫描——无新增行文本；
- 新增行行号由 hunk 头 `@@ -a,b +c,d @@` 的 c 起、按 context/added 行推进。
"""
import re

# 技术债标记：大写约定形式，词边界避免误伤如 "TODOS"/"HACKER"
TECH_DEBT_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PLUS_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")
RENAME_TO_RE = re.compile(r"^rename to (.+)$")


def _norm(entry):
    """目录条目规范化：去空白、去尾部斜杠，便于前缀匹配。"""
    return (entry or "").strip().rstrip("/")


def path_in_dir(path, dirs):
    """path 是否位于 dirs 中任一目录（或等于目录本身）。dirs 为空 → False。"""
    p = _norm(path)
    for d in dirs or []:
        d = _norm(d)
        if d and (p == d or p.startswith(d + "/")):
            return d
    return None


def scan(diff_text, source_dirs, generated_dirs):
    """扫描 unified diff 文本，返回发现列表（按 技术债 → 越界 排序，各自稳定序）。

    每条发现为 dict：
      {"type": "tech-debt", "path": <文件>, "line": <新行号>, "tag": <标记>, "text": <行内容>}
      {"type": "out-of-bounds", "path": <文件>, "reason": <含目录属性的说明>}
    干净 diff → 返回 []（由 render 输出严格"无"）。
    """
    findings = []
    changed_files = []  # 保序去重
    current_file = None
    new_lineno = None

    for raw in (diff_text or "").splitlines():
        m = HUNK_RE.match(raw)
        if m:
            new_lineno = int(m.group(1))
            continue
        m = PLUS_FILE_RE.match(raw)
        if m:
            current_file = m.group(1)
            if current_file not in changed_files:
                changed_files.append(current_file)
            continue
        m = RENAME_TO_RE.match(raw)
        if m:
            if m.group(1) not in changed_files:
                changed_files.append(m.group(1))
            continue
        if raw.startswith("+++"):  # +++ /dev/null（被删除侧）：重置当前文件
            current_file = None
            continue
        if raw.startswith("-"):  # 删除行：新行号不推进，不扫（只扫新增行）
            continue
        if raw.startswith("+"):
            if current_file and new_lineno is not None:
                m = TECH_DEBT_RE.search(raw[1:])
                if m:
                    findings.append({
                        "type": "tech-debt", "path": current_file,
                        "line": new_lineno, "tag": m.group(1),
                        "text": raw[1:].strip(),
                    })
            if new_lineno is not None:
                new_lineno += 1
            continue
        if raw.startswith(" ") or raw == "":  # context 行推进新行号
            if new_lineno is not None:
                new_lineno += 1
            continue
        # 其余（diff --git / index / mode / Binary files ...）不处理

    # 越界检查：改动文件 ∉ source_dirs 或 ∈ generated_dirs（指明目录属性）
    for path in changed_files:
        gen = path_in_dir(path, generated_dirs)
        if gen:
            findings.append({
                "type": "out-of-bounds", "path": path,
                "reason": "位于 generated_dirs（%s）——生成目录禁止手改，应改规格重新生成" % gen,
            })
            continue
        if not path_in_dir(path, source_dirs):
            findings.append({
                "type": "out-of-bounds", "path": path,
                "reason": "不在 source_dirs——越界改动（改动只允许落在 source_dirs 内）",
            })

    return findings


def render(findings, task_ref, base, head):
    """把发现渲染为 markdown REFLECT 记录段（可直接贴 TASK 备注）。

    无发现时输出**严格为"无"**（不多不少，防样板发现，
    对齐 autoloop-boot §9"无真实问题就写'无'"纪律）。
    """
    if not findings:
        return "无"
    lines = [
        "## REFLECT（机械项）— %s" % task_ref,
        "",
        "- diff 范围: %s..%s" % (base, head),
        "- 扫描器: cli/reflect（engine.md §4 机械项，TASK-096）",
        "",
        "### 发现",
        "",
    ]
    for f in findings:
        if f["type"] == "tech-debt":
            lines.append("- [技术债] `%s:%d` %s: %s" % (f["path"], f["line"], f["tag"], f["text"]))
        else:
            lines.append("- [越界] `%s` — %s" % (f["path"], f["reason"]))
    lines += [
        "",
        "### 边界",
        "",
        "以上为机械扫描**建议**，贴回 TASK 备注；不拦截状态流转（机械否决权只在 verify 与治理闸门）。",
        "判断性自省（架构违规/测试覆盖/规格对齐）仍由模型完成。",
    ]
    return "\n".join(lines)
