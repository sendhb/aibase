"""
autoloop_coder.py — Python 版 autoloop-coder 循环核心（TASK-023，迁移设计 TASK-088）

把 Bash 版 `kit/cli/autoloop-coder` 的循环逻辑迁到 Python：选任务、P0 block、
rework block、fast-path 判定、assignee 写入、prompt 构建、LLM 调用、心跳/事件。
行为与旧 Bash **逐项一致**（迁移设计红线：状态变化与 autoloop-coder-events.jsonl
事件一致），供 TASK-025 启动器 import 复用；`kit/cli/autoloop-coder` 入口自
TASK-026 起为 Python 薄 shim（dispatcher 以 `python kit/cli/autoloop-coder
--once` 调用）。

复用共享模块（TASK-021/022，纯 stdlib）：
  - tasklib: pick_task / find_task / load_task / p0_missing_approval / rework_exceeded
             / is_fast_path / write_assignee / fm_get / today
  - events : heartbeat / emit_event（autoloop-<name>-events.jsonl，格式与旧版逐字节一致）
  - llm    : run_llm_with_events / _provider_argv / _deepseek_env / _kill
             / TIMEOUT_EXIT / EXIT_NOT_FOUND
  - lock   : _open_lock / _acquire / _release（进程级防重入，TASK-012）

Windows provider 包装（TASK-022 备注"provider 解析/包装留给 TASK-088/089 调用层"）：
  - `pi` 在 Windows 是 npm `.cmd` 代理，Python `Popen(["pi", ...])` 无法直接执行；
    调用层定位同目录无扩展名 POSIX script（`#!/bin/sh`），经 Git Bash
    `bash <script> -p <prompt> --no-session` 单 argv 传参（无 shell 拼接注入，与旧
    Bash 一致）；找不到 bash/script 时回落 `llm._provider_argv`（Popen → 127）。

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py、lib/events.py 一致）。
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

# Windows 控制台 GBK 无法编码 ▶ 等字符 → 统一 UTF-8 + replace（TASK-002/TASK-011 修复）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import events
import llm
import lock
import tasklib

NAME = "coder"
# agent id（写入 assignee）默认值与旧 Bash 入口对齐（TASK-029）：
# kit/cli/autoloop-coder:45 `AGENT_ID="${AUTOLOOP_CODER_ID:-autoloop-coder}"`。
# NAME 保持 "coder" 不动：它仅构成心跳/事件文件名 `autoloop-<name>.*`，
# 与旧版 `autoloop-coder.heartbeat` 恰好一致。
AGENT_ID = "autoloop-coder"
DEFAULT_INTERVAL = 300
DEFAULT_TIMEOUT = 1800
DEFAULT_LLM = "pi"


# ---------------- 参数解析 ----------------

def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="autoloop-coder",
        description="Python 版 autoloop-coder（TASK-023）：无人值守 Coder 循环",
    )
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                    help="常驻轮询间隔（秒），默认 %d" % DEFAULT_INTERVAL)
    ap.add_argument("--once", action="store_true",
                    help="只跑一轮（不常驻轮询）")
    ap.add_argument("--unattended", action="store_true",
                    help="claude 专用：追加 --dangerously-skip-permissions")
    ap.add_argument("--id", default=os.environ.get("AUTOLOOP_CODER_ID", AGENT_ID),
                    help="agent id（写入 assignee），默认 $AUTOLOOP_CODER_ID 或 %s" % AGENT_ID)
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
    """调用 task CLI（`python kit/cli/task <cmd> ...`），与旧 Bash `${TASK_CMD[@]}` 一致。

    task CLI 是 Python 3 脚本：用当前解释器（sys.executable）调用，Windows/Linux 通用；
    输出捕获后经 log() 落入 autoloop 日志（旧版 `>> LOG 2>&1`）。
    """
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


# ---------------- Windows provider 包装 ----------------

def _find_pi_posix_script():
    """定位 pi 的无扩展名 POSIX script（Windows npm 代理 pi.CMD 同目录）。

    `shutil.which("pi")` 在 Windows 返回 `.../pi.CMD`（.cmd 代理无法被 Popen 直接执行）；
    同目录的 `pi`（无扩展名，`#!/bin/sh`）是真实入口，可被 Git Bash 单 argv 调用。
    返回 script 绝对路径；找不到返回 None。
    """
    w = shutil.which("pi")
    if not w:
        return None
    d = os.path.dirname(w)
    for cand in ("pi",):
        p = os.path.join(d, cand)
        if os.path.isfile(p) and not p.lower().endswith((".cmd", ".bat", ".exe")):
            return p
    return None


def _resolve_provider_argv(provider, prompt, unattended=False):
    """provider + prompt → (argv, env)。Windows 上 pi 经 Git Bash 执行 POSIX script。

    非 Windows 或非 pi → 回落 `llm._provider_argv`（与 TASK-022 的 provider 语义一致）；
    deepseek env 由 `llm._deepseek_env()` 注入（key 只经子进程 env，不打印）。
    """
    p = (provider or "").strip().lower()
    if os.name == "nt" and p == "pi":
        script = _find_pi_posix_script()
        if script and shutil.which("bash"):
            return ["bash", script, "-p", prompt, "--no-session"], None
    argv = llm._provider_argv(provider, prompt, unattended=unattended)
    env = llm._deepseek_env() if p == "deepseek" else None
    return argv, env


def _run_argv(argv, env, timeout=None, stdout=None, stderr=None):
    """运行自定义 argv，返回退出码（0/124/127/其它）。等价 llm.run_llm 的 Popen 语义。

    超时杀子进程返回 124；可执行文件缺失返回 127（与旧 Bash `exec` 一致）。
    """
    try:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env)
    except FileNotFoundError:
        print("✗ run_llm: 找不到可执行文件: %s" % argv[0], file=sys.stderr)
        return llm.EXIT_NOT_FOUND
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        llm._kill(proc)
        return llm.TIMEOUT_EXIT


def run_llm_coder(provider, prompt, *, log_dir, task, timeout=None,
                  unattended=False, log_file=None):
    """运行一次 LLM provider 并写 ok/timeout/error 事件（供 run_once 使用）。

    等价旧 autoloop-coder 的调用链：
      `timeout -k 30 $TIMEOUT bash lib/llm.sh <provider> <prompt> >> LOG 2>&1 || rc=$?`
      + `emit_event <task> ok|timeout|error`
    复用 `llm.run_llm_with_events` 的事件语义；argv 经 `_resolve_provider_argv`
    支持 Windows pi 包装。
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


# ---------------- prompt ----------------

def build_prompt(task_id, task_basename, fast_path, closure_step):
    """构建 Coder 提示词（与旧 Bash autoloop-coder 文本一致）。

    fast_path: True/False（prompt 中显示为 1/0，与旧 `$FAST_PATH` 一致）；
    closure_step: "done"（fast-path）或 "review"（完整路径）。
    """
    fp = "1" if fast_path else "0"
    return (
        "你现在扮演 Coder 角色。任务：%s（见 runtime/tasks/%s）。\n"
        "先读 kit/cli/autoloop-boot.md（速查：执行闭环/分级治理/返工上限/权限红线/常见坑），再读本任务与相关模块。\n"
        "最小读取：只读本任务涉及的模块，禁止全仓扫描或 knowledge/ 通读；命中规格变更再展开 aios/execution/sdd-workflow.md。\n"
        "如果 verify 失败，只读 runtime/logs/fail-<date>.log 的最后 100 行定位问题，禁止全量读日志；修复后重新 verify。\n"
        "提交 review 前逐条对照验收标准自检，确认每条可验证满足；无法确认的先修复再 verify，不要为了省事直接 review。\n"
        "执行：\n"
        "1. 制定计划（简单任务可跳过显式计划）\n"
        "2. 实现（只改 config.source_dirs，绝不手动改 config.generated_dirs）\n"
        "3. python kit/cli/task verify %s（真实执行 build/lint/test/check 并生成 VERIFY 记录，不要手写这份记录）\n"
        "4. 通过后执行: python kit/cli/task %s %s\n"
        "   - fast-path（%s=1）：直接 done，不要 review\n"
        "   - 完整路径：review（等待独立 Reviewer 会话审查）\n"
        "完整路径任务不要自行 approve 或 done —— 审查是 Reviewer 的职责，必须是另一个会话。\n"
        "如果必须动 P0 风险文件才能继续，执行: python kit/cli/task block %s \"需要人工批准: <原因>\" 然后停止。\n"
        "注意：task 是 Python 3 脚本，用 python kit/cli/task 调用（Windows 无 python3；"
        "Linux 也可用 ./kit/cli/task 或 python3），**不要用 bash kit/cli/task**"
        "（巨量输出死循环，脚本已自带防护）。"
    ) % (task_id, task_basename, task_id, closure_step, task_id, fp, task_id)


# ---------------- 主循环 ----------------

def run_once(root, opts, log_file, log_dir, task_cli_fn=None, llm_fn=None):
    """跑一轮 coder 循环；返回 LLM 退出码（或无任务/blocked 时 0）。

    参数（可注入便于隔离测试）：
      root        —— 项目根（含 aios.config.yaml）
      opts        —— parse_args 结果
      log_file    —— autoloop 日志文件路径（None 则只打 stdout）
      log_dir     —— runtime/logs（心跳/事件目录）
      task_cli_fn —— 覆盖 task CLI 调用（默认 _task_cli；测试用 stub）
      llm_fn      —— 覆盖 LLM 调用（默认 run_llm_coder；测试用 stub）
    """
    task_cli_fn = task_cli_fn or _task_cli
    llm_fn = llm_fn or run_llm_coder

    tasks_dir = os.path.join(root, "runtime", "tasks")
    events.heartbeat(log_dir, NAME)  # 每轮开始写心跳（mtime 判活，红线不变）

    task_id = tasklib.pick_task(tasks_dir)
    if not task_id:
        log("无待处理任务，跳过本轮", log_file)
        events.emit_event(log_dir, NAME, "-", "no_task")
        return 0

    task_file = tasklib.find_task(task_id, tasks_dir)
    if not task_file:
        log("⚠ %s 找不到任务文件，跳过" % task_id, log_file)
        return 0
    _, fm = tasklib.load_task(task_file)

    # P0 且无 approval-ref → 转 blocked 等人工批准（不自动实现）
    if tasklib.p0_missing_approval(fm):
        log("⚠ %s 是 P0 且无 approval-ref，转 blocked 等人工批准（不自动实现）" % task_id, log_file)
        task_cli_fn(root, "block", task_id,
                    "P0 任务需要人工先在 approval-ref 记录批准，autoloop-coder 不会自动实现")
        events.emit_event(log_dir, NAME, task_id, "blocked_p0")
        return 0

    # 返工上限：rework-count ≥ 3（已返工 2 次）不再自动实现，升级人工（TASK-047）
    if tasklib.rework_exceeded(fm):
        log("⚠ %s 返工已达上限，升级人工（不自动实现）" % task_id, log_file)
        task_cli_fn(root, "block", task_id,
                    "返工已达 2 次上限，需人工介入（拆分任务或记录介入原因后调整 rework-count 继续）")
        events.emit_event(log_dir, NAME, task_id, "blocked_rework")
        return 0

    # fast-path 判定（tasklib 与 task-policy 分级治理一致）
    fast_path = tasklib.is_fast_path(fm)
    closure_step = "done" if fast_path else "review"

    status = tasklib.fm_get(fm, "metadata.status")
    if status == "open":
        task_cli_fn(root, "start", task_id)

    # 记录 assignee，供 autoloop-reviewer 跳过"生成者=审查者"的任务
    tasklib.write_assignee(task_file, opts.id)

    log("▶ 开始实现 %s（fast_path=%d）" % (task_id, 1 if fast_path else 0), log_file)
    prompt = build_prompt(task_id, os.path.basename(task_file), fast_path, closure_step)
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

    等价旧 Bash lock_guard.sh：非阻塞排他锁，持锁覆盖整个运行期，
    进程退出/被杀时 OS 自动释放（msvcrt/fcntl 跨平台）。
    """
    lock_dir = os.path.join(root, "runtime", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    lockfile = os.path.join(lock_dir, "autoloop-coder.lock")
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
    log_file = os.path.join(log_dir, "autoloop-coder-%s.log" % tasklib.today())

    held = _acquire_lock(root)
    if held is None:
        return 1
    _, fd = held
    try:
        if opts.once:
            return run_once(root, opts, log_file, log_dir)
        log("autoloop-coder 启动（id=%s, interval=%ss, unattended=%s）"
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
