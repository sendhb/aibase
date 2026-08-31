"""
tasklib.py — 任务追踪核心逻辑（任务读写/状态机/治理判断），供 cli/task 与 autoloop 共享 import

设计（TASK-021；迁移设计 TASK-020 → docs/PYTHON-CLI-MIGRATION.md §TASK-086）：
- 只放"可复用"的核心：项目根、TASK frontmatter 解析、任务查找/选取、状态机、
  VERIFY/REVIEW 证据校验、fast-path / P0 / rework 治理、assignee 写入、事件追加。
- 目录路径（tasks/verification/reviews/logs）一律显式传参，不读模块级全局：
  cli/task（测试会覆写目录）与 autoloop（以自身运行时目录 import）都能复用。
- 纯 stdlib，零外部依赖（与 lib/config.py 一致）。
"""
import datetime
import json
import os
import re
import subprocess
import sys

STATUSES = ["open", "in-progress", "in-review", "blocked", "done", "cancelled"]
REWORK_LIMIT = 2  # 自动返工上限：rework-count 0→1→2 允许，2→3 拒绝（TASK-047）
TRANSITIONS = {
    "open": ["in-progress", "blocked", "cancelled"],
    "in-progress": ["in-review", "done", "blocked", "cancelled"],
    "in-review": ["done", "in-progress", "blocked"],
    "blocked": ["in-progress"],
    "done": [],
    "cancelled": [],
}
TASK_RE = re.compile(r"^TASK-(\d{3})-[a-z0-9-]+\.md$")


def find_project_root(start):
    """从脚本位置向上寻找项目根（含 aios.config.yaml 的目录）。

    兼容两种布局：
      - 平铺：<root>/cli/task （当前库自举，向上 2 级）
      - kit 子目录：<root>/kit/cli/task（向上 3 级）
    以 aios.config.yaml 所在目录为准，避免硬编码层级。
    """
    d = os.path.dirname(os.path.abspath(start))
    while True:
        if os.path.isfile(os.path.join(d, "aios.config.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    # 回退：向上两级（kit/cli/task → repo root 的兼容层）
    return os.path.dirname(os.path.dirname(os.path.abspath(start)))


def today():
    return datetime.date.today().isoformat()


# ---------------- 解析 / 查找 ----------------

def parse_frontmatter(text):
    """返回 (flat_dict, body_start_index)。flat_dict 键如 'name', 'metadata.status'。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, 0
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, 0
    fm, section = {}, None
    for line in lines[1:end]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", s)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "metadata":
            section = "metadata"
            continue
        if val == "":
            continue
        if val.startswith("[") and val.endswith("]"):
            val = val[1:-1].split(",") if val[1:-1].strip() else []
            val = [v.strip() for v in val]
        full = f"{section}.{key}" if section else key
        fm[full] = val
    return fm, end


def load_task(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    fm, _ = parse_frontmatter(text)
    return text, fm


def task_files(tasks_dir):
    if not os.path.isdir(tasks_dir):
        return []
    return sorted(f for f in os.listdir(tasks_dir) if TASK_RE.match(f))


def find_task(task_id, tasks_dir):
    for f in task_files(tasks_dir):
        if f.startswith(task_id + "-") or f == task_id + ".md":
            return os.path.join(tasks_dir, f)
    return None


def pick_task(tasks_dir):
    """选第一个 open 或 in-progress 任务（按文件名升序），返回短 id（如 TASK-001）；无则 None。

    替代旧 Bash autoloop-coder 的 `field() + pick_task()` grep/awk 实现（TASK-021），
    autoloop 通过 import 使用，不再用 shell 文本抽取。
    """
    for f in task_files(tasks_dir):
        _, fm = load_task(os.path.join(tasks_dir, f))
        if not fm:
            continue
        if fm.get("metadata.status") in ("open", "in-progress"):
            return short_id(fm.get("name")) or short_id(f[:-3]) or f[:-3]
    return None


def short_id(ref):
    m = re.match(r"^(TASK-\d{3})", ref or "")
    return m.group(1) if m else None


def fm_get(fm, key):
    """从扁平 frontmatter 字典取字段：兼容 'status' / 'metadata.status' 两种写法。"""
    if not fm:
        return None
    if key in fm:
        return fm[key]
    if key.startswith("metadata."):
        return fm.get(key)
    return fm.get(f"metadata.{key}")


# ---------------- 治理判断（autoloop 复用） ----------------

def status_of(fm):
    return fm_get(fm, "metadata.status")


def needs_review(fm, require_review=False):
    """分级治理：P0/P1 或指定 reviewer 或显式 require_review → 需要独立 reviewer。"""
    risk = (fm_get(fm, "risk") or "P2").upper()
    priority = (fm_get(fm, "priority") or "P2").upper()
    reviewer = (fm_get(fm, "reviewer") or "any").strip()
    return (require_review or risk in ("P0", "P1") or priority in ("P0", "P1")
            or reviewer not in ("", "any", "none"))


def is_fast_path(fm):
    """fast-path 判定（与 task-policy 分级治理一致）：risk/priority 非 P0/P1 且未指定 reviewer。"""
    return not needs_review(fm, require_review=False)


def p0_missing_approval(fm):
    """P0 任务缺人工批准引用（approval-ref 为 none/空/n/a）→ 不可自动实现，转 blocked。"""
    risk = (fm_get(fm, "risk") or "P2").upper()
    approval = (fm_get(fm, "approval-ref") or "none").strip().lower()
    return risk == "P0" and approval in ("", "none", "n/a")


def rework_count(fm):
    raw = str(fm_get(fm, "rework-count") or "0").strip()
    try:
        return int(raw)
    except ValueError:
        return 0


def rework_exceeded(fm):
    """返工已达上限（rework-count ≥ REWORK_LIMIT+1 = 3）→ 升级人工，不自动实现（TASK-047）。"""
    return rework_count(fm) > REWORK_LIMIT


def write_assignee(path, assignee):
    """写 assignee 字段（替代旧 Bash `sed -i`，TASK-021）；无字段时在 depends-on 后插入。

    返回是否发生变更（幂等：已相同则返回 False）。
    """
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    # 函数式替换：值含 \ 或 \g 等反斜杠序列时不会被 re 当作替换组解析（SMELL-001 加固）
    if re.search(r"^\s*assignee:\s*", text, re.M):
        new_text = re.sub(r"^(\s*assignee:\s*).*$",
                          lambda m: m.group(1) + str(assignee),
                          text, count=1, flags=re.M)
    else:
        new_text, n = re.subn(r"^(\s*depends-on:.*)$",
                              lambda m: m.group(1) + "\n  assignee: " + str(assignee),
                              text, count=1, flags=re.M)
        if n == 0:
            return False
    if new_text == text:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    return True


# ---------------- 证据校验 ----------------

def evidence_exists(directory, prefix, ref, record_type, ref_date=None):
    """Return true only for a current, passing, well-formed evidence record.

    ref_date: 证据日期必须匹配的日期（默认 today()）。校验 done 任务时传任务
    metadata.updated（完成当天），避免历史任务被"运行日"误判；done/approve
    路径发生在当下，仍用 today()。
    """
    if not os.path.isdir(directory):
        return False
    short = short_id(ref)
    ref_date = ref_date or today()
    for filename in os.listdir(directory):
        if not filename.startswith(prefix) or not filename.endswith(".md"):
            continue
        path = os.path.join(directory, filename)
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        if not raw.lstrip().startswith("---"):
            continue
        fm, _ = parse_frontmatter(raw)
        if not fm or fm.get("metadata.type") != record_type:
            continue
        # required fields: name, description, date, task-ref, result
        if not fm.get("name") or not fm.get("description"):
            continue
        if not fm.get("metadata.verifier") or not fm.get("metadata.commit"):
            continue
        if fm.get("metadata.date") != ref_date or fm.get("metadata.result") != "pass":
            continue
        task_ref = fm.get("metadata.task-ref", "")
        if task_ref == ref or (short and task_ref == short):
            return fm
    return False


def verify_exists(ref, verify_dir, ref_date=None):
    return bool(evidence_exists(verify_dir, "VERIFY-", ref, "verify", ref_date))


def review_exists(ref, review_dir, task_fm=None, ref_date=None):
    if not os.path.isdir(review_dir):
        return False
    short = short_id(ref)
    task_author = ((task_fm or {}).get("metadata.assignee") or "").strip()
    ref_date = ref_date or today()
    for filename in os.listdir(review_dir):
        if not filename.startswith("REVIEW-") or not filename.endswith(".md"):
            continue
        with open(os.path.join(review_dir, filename), encoding="utf-8") as fh:
            fm, _ = parse_frontmatter(fh.read())
        if not fm or fm.get("metadata.type") != "review":
            continue
        if fm.get("metadata.date") != ref_date or fm.get("metadata.result") != "pass":
            continue
        task_ref = fm.get("metadata.task-ref", "")
        if task_ref != ref and not (short and task_ref == short):
            continue
        reviewer = (fm.get("metadata.reviewer") or "").strip()
        author = (fm.get("metadata.implementation-author") or "").strip()
        if reviewer and author and reviewer != author and (not task_author or task_author == "any" or reviewer != task_author):
            return True
    return False


def check_done_prereqs(path, fm, verify_dir, review_dir, require_review=False):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    unchecked = [l for l in text.splitlines() if re.match(r"^\s*- \[ \]", l)]
    if unchecked:
        die(f"还有 {len(unchecked)} 条验收标准未勾选:\n" + "\n".join("  " + l.strip() for l in unchecked[:8]))
    ref = fm.get("name")
    name = os.path.basename(path)[:-3]
    if not (ref and verify_exists(ref, verify_dir)) and not verify_exists(name, verify_dir):
        die(f"未找到引用 {ref or name} 的 VERIFY 记录（runtime/verification/）。先写验证记录，或加 --force。")
    # 审查前置：任务处于 in-review，或 reviewer 明确指定（非 any/none/空）时，必须有 REVIEW 记录
    risk = (fm.get("metadata.risk") or "P2").upper()
    if risk == "P0" and (fm.get("metadata.approval-ref") or "none").strip().lower() in ("", "none", "n/a"):
        die(f"{ref or name}: P0 任务缺少人工批准引用 metadata.approval-ref")
    reviewer = (fm.get("metadata.reviewer") or "any").strip()
    priority = (fm.get("metadata.priority") or "P2").upper()
    must_review = require_review or risk in ("P0", "P1") or priority in ("P0", "P1") or reviewer not in ("", "any", "none")
    if must_review and not (ref and review_exists(ref, review_dir, fm)) and not review_exists(name, review_dir, fm):
        why = "任务处于 in-review" if require_review else f"reviewer={reviewer}"
        die(f"{ref or name}: {why}，但未找到 REVIEW 记录（runtime/reviews/ 中 task-ref 引用）。"
            f"先写审查记录（task review → runtime/reviews/REVIEW-xxx.md → task approve），或加 --force。")


def die(msg, code=1):
    print(f"✗ {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------- 状态机 ----------------

def set_status(path, new_status, verify_dir, review_dir, log_dir, root=None,
               force=False, require_review=False, ev=None, reason=None):
    text, fm = load_task(path)
    if fm is None:
        die(f"frontmatter 缺失或损坏: {path}")
    cur = fm.get("metadata.status")
    if cur == new_status:
        print(f"已是 {new_status}: {os.path.basename(path)}")
        return
    if not force and new_status not in TRANSITIONS.get(cur or "", []):
        die(f"非法状态转换: {cur} → {new_status}（允许: {TRANSITIONS.get(cur or '', [])}）")
    if new_status == "done" and not force:
        # 处于 in-review 的任务直接 done 也强制要求审查记录
        check_done_prereqs(path, fm, verify_dir, review_dir,
                           require_review=require_review or cur == "in-review")
    lines = text.splitlines()
    out = []
    updated = False
    for line in lines:
        if re.match(r"^\s*status:\s*", line) and not updated:
            out.append(f"  status: {new_status}")
            updated = True
            continue
        if re.match(r"^\s*updated:\s*", line):
            out.append(f"  updated: {today()}")
            continue
        out.append(line)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    print(f"✓ {os.path.basename(path)} → {new_status}")
    if ev:
        append_event(ev, os.path.basename(path), log_dir=log_dir, root=root,
                     from_status=cur, to_status=new_status, reason=reason)


def bump_rework(path, task_id):
    """in-review → in-progress（打回）时递增 metadata.rework-count；超限拒绝。

    首次开始（open → in-progress）不计数。返回新值；未打回时返回 None。
    """
    _, fm = load_task(path)
    if (fm or {}).get("metadata.status") != "in-review":
        return None
    rework = 0
    raw = (fm.get("metadata.rework-count") or "0").strip()
    try:
        rework = int(raw)
    except ValueError:
        rework = 0
    new_rework = rework + 1
    if new_rework > REWORK_LIMIT:
        die(f"{task_id}: 返工已达上限（{REWORK_LIMIT} 次，第 {new_rework} 次打回被拒绝）。"
            f"请人工介入：拆分任务，或记录介入原因后调整 metadata.rework-count 继续。")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if re.search(r"^\s*rework-count:\s*", text, re.M):
        text = re.sub(r"^(\s*rework-count:\s*).*$", r"\g<1>" + str(new_rework), text, count=1, flags=re.M)
    else:
        # 旧任务无该字段：在 depends-on 后插入
        text = re.sub(r"^(\s*depends-on:.*)$", r"\1\n  rework-count: " + str(new_rework), text, count=1, flags=re.M)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"✓ 打回计数 rework-count: {rework} → {new_rework}")
    return new_rework


# ---------------- 事件（task-events.jsonl outbox） ----------------

def _git_commit(root):
    if not root:
        return "n/a"
    try:
        proc = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        out = proc.stdout.strip()
        return out if proc.returncode == 0 and out else "n/a"
    except FileNotFoundError:
        return "n/a"


def events_path(log_dir):
    """事件文件路径：<log_dir>/task-events.jsonl（运行时数据，gitignored）。"""
    return os.path.join(log_dir, "task-events.jsonl")


def _event_last_seq(path):
    """读事件文件末尾段，解析最后一条合法 seq；文件缺失/不可读/损坏返回 0。

    只扫尾部（默认 8 KiB 窗口）避免大文件全量读；seq 单调递增由单写者
    （cli/task）追加写保证，读取段内的最后一条即为最新。
    """
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return 0
    seq = 0
    for line in tail.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and isinstance(data.get("seq"), int):
                seq = data["seq"]
        except ValueError:
            continue
    return seq


def append_event(ev, task_id, log_dir, root=None, from_status=None, to_status=None,
                 reason=None, dispatch_ref=None):
    """向 <log_dir>/task-events.jsonl 追加一条不可变事件（outbox 本地日志）。

    TASK-065：事件是审计层与未来增量推送（TASK-066）的数据源；
    失败只告警不阻断任务操作（任务文件已写入，事件缺失不应阻塞主流程）。
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = events_path(log_dir)
        seq = _event_last_seq(path) + 1
        event = {
            "seq": seq,
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "ev": ev,
            "task": short_id(task_id) or task_id,
            "from": from_status,
            "to": to_status,
            "actor": os.environ.get("AIOS_ACTOR") or "cli/task",
            "commit": _git_commit(root),
            "dispatch_ref": dispatch_ref,
            "reason": reason,
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as e:
        print(f"⚠ 事件追加失败（不影响任务操作）: {e}", file=sys.stderr)


def validate_events(log_dir):
    """校验 task-events.jsonl：每行合法 JSON、必填字段、seq 单调递增。

    返回 (errors, total)：错误数与总条数。文件缺失视为 0 条 0 错。
    """
    path = events_path(log_dir)
    if not os.path.exists(path):
        return 0, 0
    errors, total, last_seq = 0, 0, 0
    required = ("seq", "ts", "ev", "task")
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                total += 1
                try:
                    data = json.loads(line)
                except ValueError:
                    print(f"✗ task-events.jsonl:{lineno} 非合法 JSON")
                    errors += 1
                    continue
                if not isinstance(data, dict):
                    print(f"✗ task-events.jsonl:{lineno} 必须是 JSON 对象")
                    errors += 1
                    continue
                missing = [k for k in required if k not in data]
                if missing:
                    print(f"✗ task-events.jsonl:{lineno} 缺少必填字段 {missing}")
                    errors += 1
                seq = data.get("seq")
                if isinstance(seq, bool) or not isinstance(seq, int):
                    print(f"✗ task-events.jsonl:{lineno} seq 必须是整数")
                    errors += 1
                elif seq <= last_seq:
                    print(f"✗ task-events.jsonl:{lineno} seq 不单调（{seq} ≤ 上一条 {last_seq}）")
                    errors += 1
                else:
                    last_seq = seq
    except OSError as e:
        print(f"✗ task-events.jsonl 读取失败: {e}")
        return 1, 0
    return errors, total
