"""agent_runtime.py — 读取被监控项目 runtime/ 状态（kit/tools/telemetry/ 读取层）。

供 TASK-024 payload 构造使用：输入项目路径，输出该项目的 runtime 快照：

    {
      "tasks":              [{"name": "TASK-001-x.md", "content": "..."}, ...] | None,
      "focus":              "CURRENT_FOCUS.md 原文" | None,
      "heartbeats":         [{"file": "autoloop-coder.heartbeat", "mtime": 1786892280.0}, ...] | None,
      "events":             [{"name": "autoloop-coder-events.jsonl", "content": "..."}, ...] | None,
      "verification_count": 3 | None,
      "review_count":       1 | None,
    }

容错约定（任务要求"文件缺失容错为 null"）：
- 目录缺失（如项目还没有 runtime/）→ 对应字段为 None（无数据，区别于"0 个"）。
- 目录存在但没有匹配文件 → 空列表 / 0（有数据但为空）。
- 读取永不抛异常：内容用 errors="replace" 解码，OSError 跳过，mtime 取 st_mtime。
"""
import os
import json
import time


def _read_text(path):
    """读文件原文；不可读/非 UTF-8 时也不抛异常（errors=replace）。"""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _listdir_sorted(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _count_files(d, prefix, suffix):
    """统计目录下 prefix* + suffix 文件数；目录缺失返回 None（区别于 0）。"""
    if not os.path.isdir(d):
        return None
    return sum(1 for fn in _listdir_sorted(d)
               if fn.startswith(prefix) and fn.endswith(suffix))


def read_project_runtime(project_path):
    """读取项目 runtime/ 快照。

    参数:
        project_path: 被监控项目绝对路径（可以是还不存在的目录——全部字段按缺失容错）。
    返回:
        上面 docstring 形状的 dict；永不抛异常。
    """
    rt = os.path.join(project_path, "runtime")

    # tasks: runtime/tasks/TASK-*.md（文件名 + 原文，按文件名排序）
    tasks_dir = os.path.join(rt, "tasks")
    if os.path.isdir(tasks_dir):
        tasks = []
        for fn in _listdir_sorted(tasks_dir):
            if fn.startswith("TASK-") and fn.endswith(".md"):
                tasks.append({"name": fn, "content": _read_text(os.path.join(tasks_dir, fn))})
        snapshot_tasks = tasks
    else:
        snapshot_tasks = None

    # focus: runtime/states/CURRENT_FOCUS.md 原文
    focus_path = os.path.join(rt, "states", "CURRENT_FOCUS.md")
    snapshot_focus = _read_text(focus_path) if os.path.isfile(focus_path) else None

    # heartbeats / events: 都在 runtime/logs/ 下
    logs_dir = os.path.join(rt, "logs")
    if os.path.isdir(logs_dir):
        heartbeats = []
        events = []
        for fn in _listdir_sorted(logs_dir):
            path = os.path.join(logs_dir, fn)
            if fn.endswith(".heartbeat"):
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                heartbeats.append({"file": fn, "mtime": st.st_mtime})
            elif fn.endswith("-events.jsonl"):
                events.append({"name": fn, "content": _read_text(path)})
        snapshot_heartbeats = heartbeats
        snapshot_events = events
    else:
        snapshot_heartbeats = None
        snapshot_events = None

    # 计数: runtime/verification|reviews 下 VERIFY-*.md / REVIEW-*.md
    verification_count = _count_files(
        os.path.join(rt, "verification"), "VERIFY-", ".md"
    )
    review_count = _count_files(
        os.path.join(rt, "reviews"), "REVIEW-", ".md"
    )

    return {
        "tasks": snapshot_tasks,
        "focus": snapshot_focus,
        "heartbeats": snapshot_heartbeats,
        "events": snapshot_events,
        "verification_count": verification_count,
        "review_count": review_count,
    }


# ---------------------------------------------------------------------------
# TASK-066 事件流（outbox）读取层
# ---------------------------------------------------------------------------

TASK_EVENTS_FILE = "task-events.jsonl"
PUSH_CURSOR_FILE = ".push-cursor"


def _logs_dir(project_path):
    return os.path.join(project_path, "runtime", "logs")


def read_task_events(project_path):
    """读取 runtime/logs/task-events.jsonl → [parsed dict]（跳过损坏行）。

    与 read_project_runtime 容错语义一致：
    - runtime/logs 缺失或 task-events.jsonl 缺失 → None（无数据）；
    - 文件存在但为空/全损坏 → []（有数据但为空）。
    每行必须是含整数 seq 的 JSON 对象才被接受。永不抛异常。
    """
    path = os.path.join(_logs_dir(project_path), TASK_EVENTS_FILE)
    raw = _read_text(path)
    if raw is None:
        return None
    events = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("seq"), int) and not isinstance(data.get("seq"), bool) and data.get("seq") >= 1:
            events.append(data)
    return events


def read_push_cursor(project_path):
    """读取 runtime/logs/.push-cursor → int（已确认推送的最大 seq）；缺失/非法 → None。"""
    path = os.path.join(_logs_dir(project_path), PUSH_CURSOR_FILE)
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    seq = data.get("seq") if isinstance(data, dict) else None
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def write_push_cursor(project_path, seq):
    """原子写游标文件（tmp + os.replace）。seq 必须为非负整数。"""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError(f"push cursor seq 必须是非负整数（got {seq!r}）")
    logs = _logs_dir(project_path)
    os.makedirs(logs, exist_ok=True)
    target = os.path.join(logs, PUSH_CURSOR_FILE)
    tmp = os.path.join(logs, PUSH_CURSOR_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seq": seq, "updated": time.time()}, f, sort_keys=True)
    os.replace(tmp, target)


# ---------------------------------------------------------------------------
# TASK-104 session 日志流（outbox）读取层
#
# 数据源：runtime/logs/sessions/<TASK-ID>/*.jsonl（TASK-103 落盘的 pi 会话
# transcript，append-only，消息级：message/toolCall/toolResult/thinking）。
# 游标：runtime/logs/.session-push-cursor = {"offsets": {"<TASK-ID>/<文件名>": 字节偏移}}。
# 与 TASK-066 事件流同构：本地文件是唯一真相，游标是已确认推进点，
# 推送成功才推进；文件比游标小（截断/重建）→ 从 0 全量重读（宁重推不静默丢）。
# ---------------------------------------------------------------------------

SESSIONS_DIR_NAME = "sessions"
SESSION_CURSOR_FILE = ".session-push-cursor"


def _session_file_key(task_id, filename):
    """游标键："<TASK-ID>/<文件名>"（统一正斜杠，跨平台稳定）。"""
    return f"{task_id}/{filename}"


def read_session_push_cursor(project_path):
    """读取 runtime/logs/.session-push-cursor → {key: 字节偏移}；缺失/非法 → None。

    任一条目非法 → 整体返回 None（上层视作无游标，从 0 全量重读——宁重推，不静默丢）。
    """
    path = os.path.join(_logs_dir(project_path), SESSION_CURSOR_FILE)
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    offsets = data.get("offsets")
    if not isinstance(offsets, dict):
        return None
    out = {}
    for key, off in offsets.items():
        if (not isinstance(key, str) or not key
                or isinstance(off, bool) or not isinstance(off, int) or off < 0):
            return None
        out[key] = off
    return out


def write_session_push_cursor(project_path, offsets):
    """原子写 session 游标（tmp + os.replace）。

    offsets 必须 {非空字符串: 非负整数}；非法抛 ValueError（调用方捕获告警，
    游标不推进 → 下轮重推，与服务端/本地文件幂等语义一致）。
    """
    if not isinstance(offsets, dict):
        raise ValueError("session cursor offsets 必须是 dict")
    for key, off in offsets.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"session cursor key 必须是非空字符串（got {key!r}）")
        if isinstance(off, bool) or not isinstance(off, int) or off < 0:
            raise ValueError(f"session cursor 偏移必须是非负整数（{key!r} = {off!r}）")
    logs = _logs_dir(project_path)
    os.makedirs(logs, exist_ok=True)
    target = os.path.join(logs, SESSION_CURSOR_FILE)
    tmp = os.path.join(logs, SESSION_CURSOR_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"offsets": offsets, "updated": time.time()}, f, sort_keys=True)
    os.replace(tmp, target)


def read_session_deltas(project_path, offsets=None):
    """读取 sessions/<TASK-ID>/*.jsonl 中游标偏移之后的**新增完整行**。

    返回:
        None                  — sessions 目录缺失，或目录下没有任何 *.jsonl
                                （未启用/为空 → payload 不含 session 字段）。
        (deltas, all_offsets) — 目录存在且有 jsonl 文件：
            deltas: 仅含有**新增完整行**的文件，按 key 排序：
              [{"task_id": str,          # 会话所属任务（目录名）
                "name": str,             # jsonl 文件名
                "key": str,              # "<TASK-ID>/<文件名>"（游标键）
                "start": int,            # 本轮读取起始字节（含截断重建归零）
                "lines": [{"text": 原始行文本（不含换行，UTF-8 errors=replace 解码）,
                           "end": 行末字节偏移（含换行符）}]}]
            all_offsets: 所有已扫描 jsonl 的最新完整行偏移（含无新增文件），
                         推送成功后整体覆盖写游标用。
    容错（与 read_task_events 语义一致，永不抛异常）：
    - 文件大小 < 游标偏移（截断/重建）→ 从 0 全量重读（宁重推，不静默丢）；
    - 末尾不完整行（无换行符，如 pi 正在写入）不纳入，偏移停在最后一个完整行行末；
    - 单文件 OSError → 跳过（保留旧游标条目，下轮重试）。"""
    sessions_root = os.path.join(_logs_dir(project_path), SESSIONS_DIR_NAME)
    if not os.path.isdir(sessions_root):
        return None
    try:
        task_ids = sorted(os.listdir(sessions_root))
    except OSError:
        return None
    old = offsets if isinstance(offsets, dict) else {}
    deltas = []
    all_offsets = dict(old)  # 从旧游标出发；逐文件覆盖为最新完整行偏移
    found = False
    for task_id in task_ids:
        task_dir = os.path.join(sessions_root, task_id)
        if not os.path.isdir(task_dir):
            continue
        try:
            names = sorted(os.listdir(task_dir))
        except OSError:
            continue
        for fn in names:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(task_dir, fn)
            found = True
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            key = _session_file_key(task_id, fn)
            start = old.get(key)
            if (isinstance(start, bool) or not isinstance(start, int)
                    or start < 0 or start > size):
                start = 0  # 无游标/非法/文件比游标小（截断重建）→ 全量重读
            try:
                with open(path, "rb") as f:
                    if start:
                        f.seek(start)
                    chunk = f.read()
            except OSError:
                continue  # 保留 all_offsets 中旧条目（来自 dict(old)）
            lines = []
            pos = 0
            while True:
                nl = chunk.find(b"\n", pos)
                if nl < 0:
                    break
                lines.append({
                    "text": chunk[pos:nl].decode("utf-8", errors="replace"),
                    "end": start + nl + 1,
                })
                pos = nl + 1
            # 偏移只推进到最后一个完整行行末（半行不纳入，下轮续读）
            all_offsets[key] = start + pos
            if lines:
                deltas.append({
                    "task_id": task_id,
                    "name": fn,
                    "key": key,
                    "start": start,
                    "lines": lines,
                })
    if not found:
        return None  # 目录存在但无 jsonl → 与目录缺失同语义（未启用/为空）
    deltas.sort(key=lambda d: d["key"])
    return deltas, all_offsets
