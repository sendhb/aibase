"""
llm.py — LLM 调用抽象（Python 版 llm.sh，TASK-022）

供 Python 版 autoloop（TASK-088/089）import；封装三个 provider（迁移设计
TASK-087 → docs/PYTHON-CLI-MIGRATION.md）：
  - pi      : `pi -p <prompt> --no-session`（pi coding agent，TASK-009/012 默认；
              autoloop 传 session_dir 时改用 `--session-dir`，TASK-103）
  - claude  : `claude -p <prompt> [--dangerously-skip-permissions]`
              （unattended=True 时追加跳过权限确认 flag，与旧 autoloop-coder 分支一致）
  - deepseek: 读 DEEPSEEK_API_KEY（env 优先，其次 ~/.pi/agent/models.json
              providers.deepseek.apiKey）后 `codewhale exec --auto <prompt>`

与旧 shell `timeout -k 30 ... bash lib/llm.sh ...` 的语义等价：
  - 超时 → 杀子进程并返回 124（GNU timeout 退出码约定）
  - 成功 → 返回 0；其它失败 → 返回子进程退出码

安全（security-policy Rule of Two / 迁移红线 #4）：
  - deepseek key **只经子进程 env 传递**，不写代码、不打印、不进日志；
    读取失败（models.json 缺失/损坏/无字段）静默降级为不注入 env，与旧版一致。

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py 一致）。
"""
import json
import os
import subprocess
import sys
import time

from events import emit_event  # 兄弟模块（events.py，TASK-022）；无循环依赖

EXIT_NOT_FOUND = 127  # 与 shell 约定一致：可执行文件不存在（旧 Bash `exec pi` → 127）
TIMEOUT_EXIT = 124  # 与 GNU `timeout` CLI 退出码约定一致（超时被 kill）
FATAL_EXIT = 86  # provider 不可恢复错误快速失败退出码（≠124/127；事件层按 error 记，TASK-107）
PROVIDERS = ("claude", "deepseek", "pi")

# 不可重试错误特征（小写子串匹配，TASK-107）：网关拒绝/欠费/账号隔离类错误重试
# 无意义，只会拖满 timeout 把整个 autoloop 循环拖死。刻意保守：只收网关错误码与
# HTTP 401/403 的显式形态，避免误杀正常会话输出（如任务正文恰好含 401 字样）。
FATAL_PATTERNS = (
    "gateway_error", "403004", "in arrears", "欠费",
    "http 401", "http 403", "http/1.0 401", "http/1.0 403",
    "http/1.1 401", "http/1.1 403", "http/2 401", "http/2 403",
    "401 unauthorized", "403 forbidden",
    "status code 401", "status code 403",
    "status_code: 401", "status_code: 403",
)


def scan_fatal_output(text):
    """provider 输出文本 → 命中的不可重试错误特征（小写子串）；无 → None。

    纯函数（单测锚点）；大小写不敏感。只匹配 FATAL_PATTERNS，宁可漏判
    （退化为原 timeout 行为）不可误杀正常会话。
    """
    if not text:
        return None
    low = text.lower()
    for pat in FATAL_PATTERNS:
        if pat in low:
            return pat
    return None


def _provider_argv(provider, prompt, unattended=False, session_dir=None):
    """provider + prompt → 子进程 argv 列表（列表形式，无 shell 注入）。

    session_dir 仅对 pi 生效（TASK-103）：非空时以 `--session-dir <dir>` 落全量
    会话 transcript；None（默认）保持 TASK-022 契约 `--no-session`（ephemeral）。
    两 flag 互斥（--no-session 会覆盖 --session-dir，2026-09-04 真机实验），
    实现上二选一。
    """
    p = (provider or "").strip().lower()
    if p == "pi":
        if session_dir:
            return ["pi", "-p", prompt, "--session-dir", session_dir]
        return ["pi", "-p", prompt, "--no-session"]
    if p == "claude":
        argv = ["claude", "-p", prompt]
        if unattended:
            argv.append("--dangerously-skip-permissions")
        return argv
    if p == "deepseek":
        return ["codewhale", "exec", "--auto", prompt]
    raise ValueError("未知 provider: %s（支持 %s）" % (provider, " | ".join(PROVIDERS)))


def _deepseek_env():
    """构造 deepseek 调用环境：key 只经 env 传递，绝不打印。

    优先级与旧 llm.sh 一致：
      1) 已有 DEEPSEEK_API_KEY 环境变量 → 原样使用
      2) ~/.pi/agent/models.json 的 providers.deepseek.apiKey
    读取失败静默降级（不注入 key），由 codewhale 自行处理。
    """
    env = os.environ.copy()
    if env.get("DEEPSEEK_API_KEY"):
        return env
    models = os.path.join(os.path.expanduser("~"), ".pi", "agent", "models.json")
    try:
        if os.path.isfile(models):
            with open(models, encoding="utf-8") as f:
                data = json.load(f)
            key = data.get("providers", {}).get("deepseek", {}).get("apiKey")
            if key:
                env["DEEPSEEK_API_KEY"] = str(key)
    except (OSError, ValueError, AttributeError, TypeError):
        pass  # 静默降级：与旧版 `2>/dev/null || true` 语义一致
    return env


def _kill(proc):
    """超时后杀子进程并回收（等价 `timeout -k` 的 kill 语义）。

    只 kill 直接子进程（与 timeout(1) 默认一致）；进程已退出则忽略。
    """
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait()
    except OSError:
        pass


def run_llm(provider, prompt, timeout=None, unattended=False, stdout=None, stderr=None):
    """运行一次 LLM provider 调用，返回退出码。

    参数：
      provider   —— pi | claude | deepseek（非法 → ValueError）
      prompt     —— 提示词（传给 provider CLI）
      timeout    —— 秒数；None 表示不设超时
      unattended —— claude 专用：True 时追加 --dangerously-skip-permissions
      stdout/stderr —— 传给子进程（None=继承终端；可传文件对象/DEVNULL）

    返回：
      0  成功
      124 超时（子进程已被 kill）
      127 可执行文件不存在（provider CLI 未安装）
      其它  子进程自身退出码

    跨平台说明：本函数以 argv 列表直接创建子进程（无 shell，避免注入）；
    provider 可执行文件需为真二进制/.exe 或带 shebang 的可执行脚本（POSIX 直接可用）。
    Windows 上 npm 风格的 `.cmd`/`.bat` 代理（如 pi.cmd）需经 cmd.exe 才能运行，
    而经 cmd 拼接提示词存在注入风险（旧 Bash 以单 argv 传参无此问题）——
    因此本模块不在内部绕 shell；provider 解析/包装留给 TASK-088/089 调用层处理。
    """
    argv = _provider_argv(provider, prompt, unattended=unattended)
    env = _deepseek_env() if provider.strip().lower() == "deepseek" else None
    try:
        proc = subprocess.Popen(argv, stdout=stdout, stderr=stderr, env=env)
    except FileNotFoundError:
        print("✗ run_llm: 找不到可执行文件: %s" % argv[0], file=sys.stderr)
        return EXIT_NOT_FOUND
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return TIMEOUT_EXIT


def _record_usage(role, task, provider, outcome, duration_s, output_bytes):
    """best-effort 记录一次 LLM 调用用量（镜像 Bash heartbeat.sh record_usage 语义）。

    静默失败：任何异常（兄弟模块缺失 / 写盘失败）一律吞掉，绝不影响 autoloop
    主流程 —— 与旧 Bash `record_usage ... || true` 一致。数据落
    runtime/logs/token-usage.jsonl（token_usage.append，TASK-030 接线）。
    """
    try:
        import token_usage  # 兄弟模块，延迟导入：避免 import 期副作用
        token_usage.append(role=role, task=task, provider=provider,
                           outcome=outcome, duration_s=duration_s,
                           output_bytes=output_bytes)
    except Exception:
        pass


def run_llm_with_events(provider, prompt, *, log_dir, name, task,
                        timeout=None, unattended=False, log_file=None):
    """运行 provider 并自动记录 ok / timeout / error 事件（供 Python 版 autoloop 使用）。

    等价旧 autoloop-coder 的调用链：
      `timeout -k 30 $TIMEOUT bash lib/llm.sh <provider> <prompt> >> LOG 2>&1 || rc=$?`
      + `emit_event <task> ok|timeout|error`

    参数：
      log_dir —— 事件目录（runtime/logs）
      name    —— autoloop 角色名（coder | reviewer），事件文件 autoloop-<name>-events.jsonl
      task    —— 事件 task 字段（短 id）
      log_file —— 追加写日志的文件路径；None 时子进程输出继承终端
    返回退出码；事件由本函数写入（events.emit_event）。
    另：每次调用 best-effort 记录 token 用量（_record_usage，TASK-030 接线）：
    有 log_file 时按文件增量估算 output_bytes，否则记 0（终端继承不可观测）。
    """
    out = None
    size_before = None
    start = time.monotonic()
    try:
        if log_file:
            out = open(log_file, "a", encoding="utf-8", errors="replace")
            size_before = out.tell()
        rc = run_llm(provider, prompt, timeout=timeout, unattended=unattended,
                     stdout=out, stderr=subprocess.STDOUT)
    finally:
        if out is not None:
            out.close()
    duration_s = time.monotonic() - start
    if rc == TIMEOUT_EXIT:
        outcome = "timeout"
    elif rc != 0:
        outcome = "error"
    else:
        outcome = "ok"
    emit_event(log_dir, name, task, outcome)
    output_bytes = 0
    if log_file:
        try:
            output_bytes = max(0, os.path.getsize(log_file) - size_before)
        except OSError:
            output_bytes = 0
    _record_usage(name, task, provider, outcome, duration_s, output_bytes)
    return rc
