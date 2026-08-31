"""
events.py — 心跳与 autoloop 事件日志（Python 版 heartbeat.sh，TASK-022）

替代 cli/lib/heartbeat.sh，供 Python 版 autoloop（TASK-088/089）import；
与旧 Bash 版输出**逐字节一致**（迁移设计 TASK-087 → docs/PYTHON-CLI-MIGRATION.md）：
  - heartbeat: <log_dir>/autoloop-<name>.heartbeat
      每轮循环开始时写 epoch 秒（等价旧 `date +%s`），监控端主要靠 **mtime** 判活
      （agent_runtime.py 只读 st_mtime；dispatcher/monitor.py 同语义）。
  - events   : <log_dir>/autoloop-<name>-events.jsonl
      每轮一行 JSON `{"ts": <float 秒>, "task": <短 id 或 "-">, "outcome": <枚举>}`，
      append-only；outcome ∈ no_task / blocked_p0 / blocked_rework / ok / error / timeout
      （与旧 heartbeat.sh emit_event 完全一致）。

失败降级语义与旧版一致：事件写入失败只告警、不阻断主循环（旧 Bash 在 set -e
下 return 0 降级而非杀掉 autoloop）。

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py 一致）。
"""
import json
import os
import sys
import time

# autoloop 循环产出的事件 outcome 枚举（监控端 aimonitor TASK-071 按此统计失败率）
OUTCOMES = ("no_task", "blocked_p0", "blocked_rework", "ok", "error", "timeout")


def heartbeat_path(log_dir, name):
    """心跳文件路径：<log_dir>/autoloop-<name>.heartbeat"""
    return os.path.join(log_dir, "autoloop-%s.heartbeat" % name)


def events_path(log_dir, name):
    """事件文件路径：<log_dir>/autoloop-<name>-events.jsonl"""
    return os.path.join(log_dir, "autoloop-%s-events.jsonl" % name)


def heartbeat(log_dir, name):
    """写心跳文件（内容为 epoch 秒整数，与旧 `date +%s` 一致）。

    返回文件路径。文件内容供参考，判活语义以 mtime 为准——旧版与监控端均只读
    mtime（agent_runtime.py: st_mtime），本函数保持同一行为。
    """
    os.makedirs(log_dir, exist_ok=True)
    path = heartbeat_path(log_dir, name)
    with open(path, "w", encoding="ascii") as f:
        f.write(str(int(time.time())))
    return path


def emit_event(log_dir, name, task, outcome):
    """向 autoloop-<name>-events.jsonl 追加一行事件 JSON；失败只告警不阻断。

    参数与旧 heartbeat.sh emit_event 完全一致：
      task    —— 任务短 id（TASK-xxx）或 "-"（无任务轮）
      outcome —— OUTCOMES 枚举之一
    行格式：{"ts": <float 秒>, "task": <str>, "outcome": <str>}
    """
    if outcome not in OUTCOMES:
        raise ValueError(
            "未知 outcome: %r（支持 %s）" % (outcome, " | ".join(OUTCOMES))
        )
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = events_path(log_dir, name)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": time.time(), "task": task, "outcome": outcome},
                ensure_ascii=False,
            ) + "\n")
    except OSError as e:
        print("⚠ emit_event: 事件写入失败（不影响循环）: %s" % e, file=sys.stderr)


def validate_events(log_dir, name):
    """校验 autoloop-<name>-events.jsonl：每行合法 JSON、必填字段、outcome 枚举。

    返回 (errors, total)：错误数与总行数。文件缺失视为 0 条 0 错
    （与 tasklib.validate_events 容错语义一致）。
    """
    path = events_path(log_dir, name)
    if not os.path.exists(path):
        return 0, 0
    errors, total = 0, 0
    required = ("ts", "task", "outcome")
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
                    print("✗ %s:%d 非合法 JSON" % (os.path.basename(path), lineno))
                    errors += 1
                    continue
                if not isinstance(data, dict):
                    print("✗ %s:%d 必须是 JSON 对象" % (os.path.basename(path), lineno))
                    errors += 1
                    continue
                missing = [k for k in required if k not in data]
                if missing:
                    print("✗ %s:%d 缺少必填字段 %s" % (os.path.basename(path), lineno, missing))
                    errors += 1
                if data.get("outcome") not in OUTCOMES:
                    print("✗ %s:%d outcome 非法: %r" % (os.path.basename(path), lineno, data.get("outcome")))
                    errors += 1
    except OSError as e:
        print("✗ %s 读取失败: %s" % (os.path.basename(path), e))
        return 1, 0
    return errors, total
