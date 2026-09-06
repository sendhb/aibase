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
    `bash <script> -p <prompt> [--session-dir <dir> | --no-session]` 单 argv 传参
    （无 shell 拼接注入，与旧 Bash 一致）；找不到 bash/script 时回落
    `llm._provider_argv`（Popen → 127）。

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py、lib/events.py 一致）。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import time

# Windows 控制台 GBK 无法编码 ▶ 等字符 → 统一 UTF-8 + replace（TASK-002/TASK-011 修复）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import autoloop_launcher as launcher  # noqa: E402  LLM 子进程 PID 记录/收编（TASK-099）
import context_loader  # noqa: E402  装配清单注入（TASK-094）
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


def _resolve_provider_argv(provider, prompt, unattended=False, session_dir=None):
    """provider + prompt → (argv, env)。Windows 上 pi 经 Git Bash 执行 POSIX script。

    非 Windows 或非 pi → 回落 `llm._provider_argv`（与 TASK-022 的 provider 语义一致）；
    deepseek env 由 `llm._deepseek_env()` 注入（key 只经子进程 env，不打印）。
    session_dir（TASK-103）仅 pi 生效：非空 → `--session-dir <dir>`（Windows 分支
    转正斜杠适配 Git Bash），None → `--no-session`（TASK-022 默认契约）。
    """
    p = (provider or "").strip().lower()
    if os.name == "nt" and p == "pi":
        script = _find_pi_posix_script()
        if script and shutil.which("bash"):
            argv = ["bash", script, "-p", prompt]
            if session_dir:
                argv += ["--session-dir", session_dir.replace("\\", "/")]
            else:
                argv.append("--no-session")
            return argv, None
    argv = llm._provider_argv(provider, prompt, unattended=unattended,
                              session_dir=session_dir)
    env = llm._deepseek_env() if p == "deepseek" else None
    return argv, env


def _run_argv(argv, env, timeout=None, stdout=None, stderr=None,
              root=None, llm_name=None, task_id=None, on_start=None):
    """运行自定义 argv，返回退出码（0/124/127/其它）。等价 llm.run_llm 的 Popen 语义。

    超时杀子进程返回 124；可执行文件缺失返回 127（与旧 Bash `exec` 一致）。
    root 非空时记录/清除 LLM 子进程 PID（TASK-099 孤儿收编；coder/reviewer
    共用本函数，收编点收敛在此单点）；None 时行为与旧版完全一致。
    on_start 非空时在 Popen 成功后立即以子进程 PID 回调（TASK-100 per-task log
    轮次头回填 PID；先于子进程首字节输出，不改启动方式/退出码契约），异常吞掉
    不影响会话。
    """
    try:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env)
    except FileNotFoundError:
        print("✗ run_llm: 找不到可执行文件: %s" % argv[0], file=sys.stderr)
        return llm.EXIT_NOT_FOUND
    if root is not None:
        launcher.record_llm_pid(root, proc.pid, llm_name or os.path.basename(argv[0]),
                                task_id or "-")
    if on_start is not None:
        try:
            on_start(proc.pid)
        except Exception:  # noqa: BLE001 — 轮次头失败绝不影响 LLM 会话
            pass
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        llm._kill(proc)
        rc = llm.TIMEOUT_EXIT
    finally:
        if root is not None:
            launcher.clear_llm_pid(root, pid=proc.pid)
    return rc


# ---------------- per-task 会话日志（TASK-100） ----------------

def task_log_path(task_id):
    """task ID → per-task log 相对路径（纯函数，监控/agent/人类均可推导）。

    `runtime/logs/tasks/<TASK-ID>.log`：路径是 task ID 的纯函数，无其它输入。
    task_id 须为 task CLI 生成的 `TASK-\\d+(-slug)` 形态（文件名安全）。
    实际落盘 = os.path.join(log_dir, "tasks", "<TASK-ID>.log")（log_dir 即
    runtime/logs，与本函数同一布局）。
    """
    return "runtime/logs/tasks/%s.log" % task_id


def task_session_dir(task_id):
    """task ID → per-task session 目录相对路径（纯函数，TASK-103）。

    `runtime/logs/sessions/<TASK-ID>/`：pi `--session-dir` 落点，pi 在其下自建
    `<时间戳>_<UUID>.jsonl` 全量会话（消息/tool call/thinking/usage），与
    per-task log（人读摘要，TASK-100）互补；`pi --export <file>` 可回放 HTML。
    目录名 = task ID 纯函数，monitor/agent/人类均可推导；coder/reviewer 同任务
    多轮共存同目录（pi 自建文件名带时间戳，不冲突）。
    """
    return "runtime/logs/sessions/%s" % task_id


def prepare_session_dir(provider, log_dir, task_id):
    """pi + 真实 task → 创建并返回绝对 session 目录；否则/失败返回 None（降级）。

    仅 pi 需要（claude/deepseek 无 pi session 概念）；no_task 空转轮（task 为
    空/"-"）不落 session。makedirs 失败 → None（调用方回落 --no-session，
    绝不阻塞会话）。返回绝对路径（log_dir 由 main() 以绝对 root 拼接），
    使 `--session-dir` 与 pi 子进程 cwd 无关。
    """
    if not task_id or task_id == "-":
        return None
    if (provider or "").strip().lower() != "pi":
        return None
    sdir = os.path.join(log_dir, "sessions", task_id)
    try:
        os.makedirs(sdir, exist_ok=True)
    except OSError:
        return None
    return sdir


def _open_task_log(log_dir, task_id, *, role, provider, fallback_log):
    """打开本轮 LLM 会话输出文件，返回 (句柄, on_start 回调或 None)。

    - task_id 为空/"-"（no_task 空转轮）→ 不产生 task log，回落 fallback_log
      （autoloop-<role>-<date>.log / both.log，行为与旧版一致）；无 fallback 则
      (None, None)（不重定向）。
    - 正常任务 → append 打开 per-task log（实时 tail；路径见 task_log_path），
      并返回轮次头回调：_run_argv 在 Popen 成功后以子进程 PID 调用，写入
      `=== [ISO时间] <role> round N | provider <p> | PID <pid> ===`。
      round N = 文件内既有轮次头数 + 1（跨进程可续，rework/reviewer 多轮共存
      单文件保任务完整一生）；头部先于会话输出（Popen 返回到子进程首字节的
      启动间隔远大于本回调延迟，实践无交错）。
    """
    if not task_id or task_id == "-":
        out = None
        if fallback_log:
            out = open(fallback_log, "a", encoding="utf-8", errors="replace")
        return out, None
    tdir = os.path.join(log_dir, "tasks")
    os.makedirs(tdir, exist_ok=True)
    tpath = os.path.join(tdir, "%s.log" % task_id)
    out = open(tpath, "a", encoding="utf-8", errors="replace")
    round_no = 1
    try:
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            round_no += sum(1 for ln in f if ln.startswith("=== ["))
    except OSError:
        pass  # 既有文件不可读 → 视为首轮，不影响追加
    prefix = "=== [%s] %s round %d | provider %s | PID " % (
        time.strftime("%Y-%m-%dT%H:%M:%S"), role, round_no, provider)

    def on_start(pid):
        try:
            out.write(prefix + str(pid) + " ===\n")
            out.flush()
        except (OSError, ValueError):
            pass  # 轮次头失败不致命：会话输出照常
    return out, on_start


def run_llm_coder(provider, prompt, *, log_dir, task, timeout=None,
                  unattended=False, log_file=None, root=None):
    """运行一次 LLM provider 并写 ok/timeout/error 事件（供 run_once 使用）。

    等价旧 autoloop-coder 的调用链：
      `timeout -k 30 $TIMEOUT bash lib/llm.sh <provider> <prompt> >> LOG 2>&1 || rc=$?`
      + `emit_event <task> ok|timeout|error`
    复用 `llm.run_llm_with_events` 的事件语义；argv 经 `_resolve_provider_argv`
    支持 Windows pi 包装。
    会话输出按 task 落盘（TASK-100）：`runtime/logs/tasks/<TASK-ID>.log`
    （append + 轮次头，实时可 tail）；no_task 空转轮（task 为空/"-"）回落
    log_file（daily/both.log）不产生 task log；循环层行仍走 log_file。
    pi 会话全量另落 `runtime/logs/sessions/<TASK-ID>/`（TASK-103，方案A：
    per-task log 管人读摘要 + session transcript 管回放，互补）；目录创建失败
    降级 `--no-session`，绝不阻塞会话。
    """
    session_dir = prepare_session_dir(provider, log_dir, task)
    argv, env = _resolve_provider_argv(provider, prompt, unattended=unattended,
                                       session_dir=session_dir)
    out, on_start = _open_task_log(log_dir, task, role=NAME, provider=provider,
                                   fallback_log=log_file)
    try:
        rc = _run_argv(argv, env, timeout=timeout, stdout=out,
                       stderr=subprocess.STDOUT, root=root,
                       llm_name=provider, task_id=task, on_start=on_start)
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


# ---------------- 会话断点恢复 checkpoint（TASK-095） ----------------

CHECKPOINT_TAIL_LINES = 20  # checkpoint 附带会话尾部行数（超时/异常定位用）
CHECKPOINT_ITEM_CAP = 12    # 已完成/下一步 每段条目上限（保持 checkpoint 紧凑）

# 当前进度表行：| 项 | ✅/⏳/❌ | 位置 |
_PROGRESS_ROW_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*([✅⏳❌])\s*\|")
# 验收标准勾选：- [x] / - [ ]
_CHECKBOX_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.+?)\s*$")
_ROUND_HEADER_RE = re.compile(r"^=== \[")


def checkpoint_relpath(task_id):
    """task ID → checkpoint 相对路径（纯函数）：runtime/states/STATE-PROGRESS-<task>.md。"""
    return "runtime/states/STATE-PROGRESS-%s.md" % task_id


def _exit_label(rc):
    """LLM 轮次退出码 → checkpoint 标签（ok / timeout(124) / error(N)）。"""
    if rc == llm.TIMEOUT_EXIT:
        return "timeout(%d)" % llm.TIMEOUT_EXIT
    if rc == 0:
        return "ok"
    return "error(%d)" % rc


def _extract_progress(task_text):
    """从 TASK 本体机械提取 已完成/下一步/阻塞 信号（不依赖会话自觉）。

    来源：当前进度表 ✅/⏳/❌ 行 + 验收标准 checkbox 勾选态。各段限
    CHECKPOINT_ITEM_CAP 条，保持 checkpoint 紧凑（装载时受预算分配）。
    """
    done, todo, blocked = [], [], []
    for ln in task_text.splitlines():
        m = _CHECKBOX_RE.match(ln)
        if m:
            (done if m.group(1).strip() else todo).append("- " + m.group(2))
            continue
        m = _PROGRESS_ROW_RE.match(ln.strip())
        if m:
            item, mark = m.group(1), m.group(2)
            if mark == "✅":
                done.append("- %s（当前进度表）" % item)
            elif mark == "⏳":
                todo.append("- %s（当前进度表）" % item)
            else:
                blocked.append("- %s（当前进度表 ❌）" % item)
    return (done[:CHECKPOINT_ITEM_CAP], todo[:CHECKPOINT_ITEM_CAP],
            blocked[:CHECKPOINT_ITEM_CAP])


def _log_tail_and_rounds(log_dir, task_id):
    """读 per-task log → (最后 N 行[单行截 200 字符], 轮次头计数)。

    文件缺失/不可读 → ([], 0)（不影响 checkpoint 其余段）。
    """
    path = os.path.join(log_dir, "tasks", "%s.log" % task_id)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return [], 0
    rounds = sum(1 for ln in lines if _ROUND_HEADER_RE.match(ln))
    tail = [ln[:200] for ln in lines[-CHECKPOINT_TAIL_LINES:]]
    return tail, rounds


def render_checkpoint(task_id, exit_label, rounds, done, todo, blocked, tail):
    """组装 checkpoint 文本：元信息 + 已完成/下一步/阻塞 三段 + 会话尾部。"""
    lines = [
        "# STATE-PROGRESS — %s" % task_id,
        "",
        "> 断点恢复 checkpoint：autoloop-coder 会话收尾自动写入（勿手编）。"
        "对 in-progress 任务，新会话经 `kit/cli/context` 装配时本文件排首位"
        "（断点恢复优先装载，TASK-095）。",
        "",
        "- task: %s" % task_id,
        "- updated: %s" % time.strftime("%Y-%m-%dT%H:%M:%S"),
        "- round: %d（per-task log 轮次头计数）" % rounds,
        "- last-exit: %s" % exit_label,
        "",
        "## 已完成",
        "",
        "<!-- 机械提取：TASK 当前进度表 ✅ 项 + 已勾选验收标准 -->",
    ]
    lines += (done or ["- （无机械信号）"])
    lines += ["", "## 下一步", "",
              "<!-- 机械提取：TASK 当前进度表 ⏳ 项 + 未勾选验收标准 -->"]
    lines += (todo or ["- （无机械信号）"])
    lines += ["", "## 阻塞", ""]
    lines += (blocked or [])
    if exit_label != "ok":
        lines.append("- 上一轮会话异常退出: %s" % exit_label)
    elif not blocked:
        lines.append("- （无机械信号）")
    lines += ["", "## 会话尾部（最后 %d 行原始输出）" % CHECKPOINT_TAIL_LINES,
              "", "```"]
    lines += (tail or ["（无会话输出）"])
    lines += ["```", ""]
    return "\n".join(lines)


def write_checkpoint(root, task_id, task_file, rc, log_dir):
    """会话收尾钩子：写 runtime/states/STATE-PROGRESS-<task>.md（TASK-095）。

    正常结束 / 超时(124) / 异常退出 三条路径都由 run_once 在 LLM 轮次返回后
    无条件调用（checkpoint 不再依赖会话自觉）。内容机械提取自 TASK 本体
    （进度表 + 验收勾选态）与 per-task log（轮次数 + 尾部原始输出）。
    写盘/读取失败 → stderr 告警并返回 False，**绝不阻塞 autoloop 主流程**。
    """
    try:
        with open(task_file, encoding="utf-8") as f:
            task_text = f.read()
        done, todo, blocked = _extract_progress(task_text)
        tail, rounds = _log_tail_and_rounds(log_dir, task_id)
        text = render_checkpoint(task_id, _exit_label(rc), rounds,
                                 done, todo, blocked, tail)
        states = os.path.join(root, "runtime", "states")
        os.makedirs(states, exist_ok=True)
        with open(os.path.join(states, "STATE-PROGRESS-%s.md" % task_id),
                  "w", encoding="utf-8") as f:
            f.write(text)
        return True
    except Exception as e:  # noqa: BLE001 —— checkpoint 失败绝不阻塞主流程
        print("⚠ checkpoint 写盘失败（不影响 autoloop 主流程）: %s" % e,
              file=sys.stderr)
        return False


# ---------------- prompt ----------------

def build_prompt(task_id, task_basename, fast_path, closure_step, assembly=None):
    """构建 Coder 提示词（与旧 Bash autoloop-coder 文本一致）。

    fast_path: True/False（prompt 中显示为 1/0，与旧 `$FAST_PATH` 一致）；
    closure_step: "done"（fast-path）或 "review"（完整路径）。
    assembly: context_loader 装配清单文本（TASK-094；None/空则省略该节）。
    """
    fp = "1" if fast_path else "0"
    text = (
        "你现在扮演 Coder 角色。任务：%s（见 runtime/tasks/%s）。\n"
        "先读 kit/cli/autoloop-boot.md（速查：执行闭环/分级治理/返工上限/权限红线/常见坑），再读本任务与相关模块。\n"
        "最小读取：只读本任务涉及的模块，禁止全仓扫描或 knowledge/ 通读；命中规格变更再展开 aios/execution/sdd-workflow.md。\n"
        "如果 verify 失败，只读 runtime/logs/fail-<date>.log 的最后 100 行定位问题，禁止全量读日志；修复后重新 verify。\n"
        "提交 review 前逐条对照验收标准自检，确认每条可验证满足；无法确认的先修复再 verify，不要为了省事直接 review。\n"
        "执行：\n"
        "1. 制定计划（简单任务可跳过显式计划）\n"
        "2. 实现（只改 config.source_dirs，绝不手动改 config.generated_dirs）\n"
        "3. set -o pipefail; python kit/cli/task verify %s 2>&1 | tee -a %s"
        "（真实执行 build/lint/test/check 并生成 VERIFY 记录，不要手写这份记录；"
        "verify 输出 tee 进 per-task log，运行中可实时 tail，pipefail 保真实退出码）\n"
        "4. 通过后执行: python kit/cli/task %s %s\n"
        "   - fast-path（%s=1）：直接 done，不要 review\n"
        "   - 完整路径：review（等待独立 Reviewer 会话审查）\n"
        "完整路径任务不要自行 approve 或 done —— 审查是 Reviewer 的职责，必须是另一个会话。\n"
        "如果必须动 P0 风险文件才能继续，执行: python kit/cli/task block %s \"需要人工批准: <原因>\" 然后停止。\n"
        "注意：task 是 Python 3 脚本，用 python kit/cli/task 调用（Windows 无 python3；"
        "Linux 也可用 ./kit/cli/task 或 python3），**不要用 bash kit/cli/task**"
        "（巨量输出死循环，脚本已自带防护）。"
    ) % (task_id, task_basename, task_id, task_log_path(task_id),
         closure_step, task_id, fp, task_id)
    if assembly:
        text += (
            "\n装配清单（context loader 已按预算截断；只读清单内文件，"
            "禁止全仓扫描或 knowledge/ 通读；标注 [TRUNCATED 到第 N 行] 的文件"
            "只读前 N 行）：\n%s\n"
        ) % assembly
    return text


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
    # 装配清单注入（TASK-094）：机械执行最小读取，替代纯规劝；失败 fail-open（prompt 照发）
    try:
        plan = context_loader.assemble(task_id, root=root)
        assembly = plan.render() if plan is not None else ""
    except Exception:  # noqa: BLE001 —— 装配失败不阻塞会话
        assembly = ""
    prompt = build_prompt(task_id, os.path.basename(task_file), fast_path, closure_step,
                          assembly=assembly)
    rc = llm_fn(opts.llm, prompt,
                log_dir=log_dir, task=task_id, timeout=opts.timeout,
                unattended=opts.unattended, log_file=log_file, root=root)
    if rc == llm.TIMEOUT_EXIT:
        log("✗ %s 会话超时（>%ss），已 kill" % (task_id, opts.timeout), log_file)
    elif rc != 0:
        log("✗ %s 会话异常退出，exit=%d" % (task_id, rc), log_file)
    else:
        log("■ 本轮结束 %s" % task_id, log_file)
    # 会话收尾钩子（TASK-095）：正常/超时/异常三条路径都落盘断点 checkpoint，
    # 失败只告警不阻塞（返回 False 时主流程照常）。
    if write_checkpoint(root, task_id, task_file, rc, log_dir):
        log("✓ checkpoint 已更新: %s" % checkpoint_relpath(task_id), log_file)
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
