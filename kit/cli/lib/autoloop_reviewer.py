"""
autoloop_reviewer.py — Python 版 autoloop-reviewer 循环核心（TASK-024，迁移设计 TASK-089）

把 Bash 版 `kit/cli/autoloop-reviewer` 的循环逻辑迁到 Python：选 in-review 任务、
生成者 ≠ 审查者（assignee ≠ 自己）、fast-path 跳过、prompt 构建、LLM 调用、
心跳/事件。行为与旧 Bash **逐项一致**（迁移设计红线：状态变化与
autoloop-reviewer-events.jsonl 事件一致），供 TASK-025 启动器 import 复用；
`kit/cli/autoloop-reviewer` 入口本身仍为 Bash（入口切 Python 薄 shim 与调用点
更新归 TASK-026/TASK-091）。

复用共享模块（TASK-021/022/023，纯 stdlib）：
  - tasklib : task_files / find_task / load_task / fm_get / is_fast_path / short_id
              / today / find_project_root
  - events  : heartbeat / emit_event（autoloop-reviewer-events.jsonl，格式逐字节一致）
  - llm     : TIMEOUT_EXIT / EXIT_NOT_FOUND / _provider_argv / _deepseek_env
  - lock    : _open_lock / _acquire / _release（进程级防重入，TASK-012）
  - autoloop_coder : _resolve_provider_argv / _run_argv（Windows provider 包装与
              Popen 超时杀进程语义，TASK-023 交付——两个角色是同一"调用层"，
              import 复用避免 SMELL-001 式重复维护）

Windows provider 包装：与 autoloop_coder 一致——`pi` 在 Windows 是 npm `.cmd`
代理，经 Git Bash `bash <posix-script> -p <prompt> --no-session` 单 argv 传参
（无注入）；找不到回落 `llm._provider_argv`（Popen → 127）。

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py、lib/events.py 一致）。
"""
import argparse
import os
import subprocess
import sys
import time

# Windows 控制台 GBK 无法编码 ▶ 等字符 → 统一 UTF-8 + replace（TASK-002/TASK-011 修复）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from autoloop_coder import _resolve_provider_argv, _run_argv  # noqa: E402（Windows provider 包装复用）
import events  # noqa: E402
import llm  # noqa: E402
import lock  # noqa: E402
import tasklib  # noqa: E402

NAME = "reviewer"  # 心跳/事件文件名：autoloop-reviewer.heartbeat / -events.jsonl
DEFAULT_INTERVAL = 300
DEFAULT_TIMEOUT = 1800
DEFAULT_LLM = "pi"


# ---------------- 参数解析 ----------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="autoloop-reviewer",
        description="Python 版 autoloop-reviewer（TASK-024）：无人值守 Reviewer 循环",
    )
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="常驻轮询间隔（秒），默认 %d" % DEFAULT_INTERVAL)
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮（不常驻轮询）")
    ap.add_argument("--unattended", action="store_true",
                    help="claude 专用：追加 --dangerously-skip-permissions")
    ap.add_argument("--id", default=os.environ.get("AUTOLOOP_REVIEWER_ID", "autoloop-reviewer"),
                    help="agent id（审查者身份，生成者≠审查者判据），默认 "
                         "$AUTOLOOP_REVIEWER_ID 或 autoloop-reviewer")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="LLM 会话超时（秒），默认 %d" % DEFAULT_TIMEOUT)
    ap.add_argument("--llm", default=os.environ.get("AIOS_LLM", DEFAULT_LLM),
                    help="LLM provider: pi | claude | deepseek，默认 $AIOS_LLM 或 %s" % DEFAULT_LLM)
    return ap.parse_args(argv)


# ---------------- 日志 / task CLI ----------------

def log(msg, log_file=None):
    line = "[%s] %s" % (time.strftime("%F %T"), msg)
    print(line)
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8", errors="replace") as f:
                f.write(line + "\n")
        except OSError as e:
            print("⚠ log 写入失败（不影响循环）: %s" % e, file=sys.stderr)


def _task_cli(root, *args):
    """调用 task CLI（`python kit/cli/task <cmd> ...`），与旧 Bash `${TASK_CMD[@]}` 一致。"""
    script = os.path.join(root, "kit", "cli", "task")
    try:
        proc = subprocess.run([sys.executable, script] + list(args),
                              cwd=root, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        log("✗ 找不到 task CLI: %s" % script)
        return 127
    out = (proc.stdout or "").strip()
    if out:
        log(out)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if err:
            log("✗ task %s 失败: %s" % (args[0] if args else "", err))
    return proc.returncode


# ---------------- LLM 调用（事件名 reviewer） ----------------

def run_llm_reviewer(provider, prompt, *, log_dir, task, timeout=None,
                     unattended=False, log_file=None):
    """运行一次 LLM provider 并写 ok/timeout/error 事件（供 run_once 使用）。

    等价旧 autoloop-reviewer 的调用链：
      `timeout -k 30 $TIMEOUT bash lib/llm.sh <provider> <prompt> >> LOG 2>&1 || rc=$?`
      + `emit_event <task> ok|timeout|error`
    argv 复用 autoloop_coder._resolve_provider_argv（Windows pi 包装），
    事件写入 autoloop-reviewer-events.jsonl（NAME="reviewer"）。
    """
    argv, env = _resolve_provider_argv(provider, prompt, unattended=unattended)
    out = None
    try:
        if log_file:
            out = open(log_file, "a", encoding="utf-8", errors="replace")
        rc = _run_argv(argv, env, timeout=timeout, stdout=out,
                       stderr=subprocess.STDOUT)
    finally:
        if out is not None:
            out.close()
    if rc == llm.TIMEOUT_EXIT:
        events.emit_event(log_dir, NAME, task, "timeout")
    elif rc != 0:
        events.emit_event(log_dir, NAME, task, "error")
    else:
        events.emit_event(log_dir, NAME, task, "ok")
    return rc


# ---------------- 选任务 ----------------

def pick_in_review(tasks_dir, agent_id, log_fn=None):
    """选第一个 in-review 且 assignee ≠ 自己的任务（按任务编号升序），返回短 id；无则 None。

    与旧 Bash autoloop-reviewer pick_task 逐项一致：
      - status == in-review
      - assignee 非空且 == 自己 → 跳过（生成者 ≠ 审查者）
      - fast-path（risk/priority 非 P0/P1 且 reviewer 空/any/none）→ 防御性跳过
        （正常不会进入 in-review，coder 直接 done；TASK-047/048 分级治理）
    """
    for f in tasklib.task_files(tasks_dir):
        path = os.path.join(tasks_dir, f)
        _, fm = tasklib.load_task(path)
        if not fm:
            continue
        if tasklib.fm_get(fm, "metadata.status") != "in-review":
            continue
        assignee = (tasklib.fm_get(fm, "metadata.assignee") or "").strip()
        if assignee and assignee == agent_id:
            continue  # 生成者 ≠ 审查者：跳过自己实现的任务
        if tasklib.is_fast_path(fm):
            if log_fn:
                log_fn("⏭ %s 是 fast-path（risk/priority 非 P0/P1 且未指定 reviewer），跳过审查"
                       % f[:-3])
            continue
        return tasklib.short_id(tasklib.fm_get(fm, "name")) or \
            tasklib.short_id(f[:-3]) or f[:-3]
    return None


# ---------------- prompt ----------------

def build_prompt(task_id, task_basename, agent_id, reviewer_depth):
    """构建 Reviewer 提示词（与旧 Bash autoloop-reviewer 文本一致）。

    reviewer_depth: "六维（P0/P1）" 或 "三问（P2）"（按任务 risk/priority 分级）；
    agent_id 写入 REVIEW 记录的 reviewer 字段。
    """
    return (
        "你现在扮演 Reviewer 角色。任务：%s（见 runtime/tasks/%s）。\n"
        "先读 kit/cli/autoloop-boot.md（速查）与 kit/runtime/reviews/REVIEW.template.md（分级模板）。\n"
        "你不是这个任务的实现者，本次是独立会话审查，禁止修改被审查的代码（审查 ≠ 修复）。\n"
        "审查上下文最小化：先运行 python kit/cli/lib/review_context.py %s 获取任务元数据、"
        "VERIFY 记录和 git diff 摘要；只读与 diff 相关的文件，禁止全仓扫描或 knowledge/ 通读。"
        "需要看具体 diff 时再运行 git diff --unified=5 <相关文件>。\n"
        "P2 任务先运行 python kit/cli/lib/p2_review_check.py %s；自动检查项"
        "（verify 是否存在、generated_dirs 是否被改、diff 文件清单）以脚本输出为准，"
        "不要重复检查。\n"
        "按任务 risk 分级审查（%s）：\n"
        "- P2 三问：验收标准是否满足？改动是否越界？verify 是否真实通过？（不自由审计，不做额外扩展检查）\n"
        "- P0/P1 六维：SDD 合规/架构/安全/影响/质量/测试。\n"
        "发现只写真实问题，禁止样板发现；无问题写\"无\"，不要为了填满表格制造返工。\n"
        "写 runtime/reviews/REVIEW-<date>-<scope>.md（reviewer 字段填 %s，"
        "implementation-author 填任务的 assignee，result 填 pass 或 issues-found/critical）。\n"
        "- 通过 → 执行: python kit/cli/task approve %s\n"
        "- 需要返工 → 在 REVIEW 记录写明问题，执行: python kit/cli/task start %s "
        "打回给 Coder（自动计 rework；2→3 被拒则 task block）\n"
        "- 若 approve 因 P0 缺少 approval-ref 被拒绝，执行: python kit/cli/task block %s "
        "\"P0 需人工批准\" 然后停止，不要重试或绕过\n"
        "注意：task 是 Python 3 脚本，用 python kit/cli/task 调用（Windows 无 python3；"
        "Linux 也可用 ./kit/cli/task 或 python3），**不要用 bash kit/cli/task**"
        "（巨量输出死循环，脚本已自带防护）。"
    ) % (task_id, task_basename, task_id, task_id, reviewer_depth, agent_id,
         task_id, task_id, task_id)


# ---------------- 主循环 ----------------

def run_once(root, opts, log_file, log_dir, task_cli_fn=None, llm_fn=None):
    """跑一轮 reviewer 循环；返回 LLM 退出码（或无任务/跳过时 0）。

    参数（可注入便于隔离测试）：
      root        —— 项目根（含 aios.config.yaml）
      opts        —— parse_args 结果
      log_file    —— autoloop 日志文件路径（None 则只打 stdout）
      log_dir     —— runtime/logs（心跳/事件目录）
      task_cli_fn —— 覆盖 task CLI 调用（默认 _task_cli；测试用 stub）
      llm_fn      —— 覆盖 LLM 调用（默认 run_llm_reviewer；测试用 stub）
    """
    task_cli_fn = task_cli_fn or _task_cli
    llm_fn = llm_fn or run_llm_reviewer

    tasks_dir = os.path.join(root, "runtime", "tasks")
    events.heartbeat(log_dir, NAME)  # 每轮开始写心跳（mtime 判活，红线不变）

    task_id = pick_in_review(tasks_dir, opts.id, log_fn=lambda m: log(m, log_file))
    if not task_id:
        log("无待审查任务，跳过本轮", log_file)
        events.emit_event(log_dir, NAME, "-", "no_task")
        return 0

    task_file = tasklib.find_task(task_id, tasks_dir)
    if not task_file:
        log("⚠ %s 找不到任务文件，跳过" % task_id, log_file)
        return 0
    _, fm = tasklib.load_task(task_file)

    risk = (tasklib.fm_get(fm, "risk") or "P2").upper()
    priority = (tasklib.fm_get(fm, "priority") or "P2").upper()
    if risk in ("P0", "P1") or priority in ("P0", "P1"):
        reviewer_depth = "六维（P0/P1）"
    else:
        reviewer_depth = "三问（P2）"

    log("▶ 开始审查 %s" % task_id, log_file)
    prompt = build_prompt(task_id, os.path.basename(task_file), opts.id, reviewer_depth)
    rc = llm_fn(opts.llm, prompt,
                log_dir=log_dir, task=task_id, timeout=opts.timeout,
                unattended=opts.unattended, log_file=log_file)
    if rc == llm.TIMEOUT_EXIT:
        log("✗ %s 会话超时（>%ss），已 kill" % (task_id, opts.timeout), log_file)
    elif rc != 0:
        log("✗ %s 会话异常退出，exit=%d" % (task_id, rc), log_file)
    else:
        log("■ 本轮结束 %s" % task_id, log_file)
    return rc


def _acquire_lock(root):
    """进程级防重入锁（lib/lock.py，TASK-012）；拿不到锁 → 返回 None。

    同一角色（reviewer）同时只允许一个进程，堵住两个 reviewer 并发 pick
    同一 in-review 任务 → 重复 REVIEW/重复 approve 的竞态。
    """
    lock_dir = os.path.join(root, "runtime", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lockfile = os.path.join(lock_dir, "autoloop-reviewer.lock")
    try:
        fd = lock._open_lock(lockfile)
    except OSError as e:
        log("✗ 无法打开锁文件 %s: %s" % (lockfile, e))
        return None
    if not lock._acquire(fd):
        print("✗ 锁已被占用: %s（拒绝启动，避免重复进程）" % lockfile, file=sys.stderr)
        os.close(fd)
        return None
    return (lockfile, fd)


def main(argv=None):
    opts = parse_args(argv)
    root = tasklib.find_project_root(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(root, "runtime", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "autoloop-reviewer-%s.log" % tasklib.today())

    held = _acquire_lock(root)
    if held is None:
        return 1
    _, fd = held
    try:
        if opts.once:
            return run_once(root, opts, log_file, log_dir)
        log("autoloop-reviewer 启动（id=%s, interval=%ss, unattended=%s）"
            % (opts.id, opts.interval, opts.unattended), log_file)
        while True:
            run_once(root, opts, log_file, log_dir)
            time.sleep(opts.interval)
    finally:
        lock._release(fd)
        os.close(fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
