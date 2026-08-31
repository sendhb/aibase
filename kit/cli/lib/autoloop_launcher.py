#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoloop_launcher.py — Python 版 autoloop 启动器核心（TASK-025，迁移设计 TASK-090）

把 Bash 版 `kit/cli/autoloop` 的启动逻辑迁到 Python：coder/reviewer/both/stop
四种模式 + --once/--interval/--foreground/--coder-llm/--reviewer-llm 透传。
`kit/cli/autoloop` 是薄入口（shebang + import 本模块后 main()）；本模块亦可直接
运行（`python kit/cli/lib/autoloop_launcher.py ...`，隔离测试用）。

与旧 Bash **逐项一致**（迁移设计红线：锁文件路径一致、PID 文件语义不变、
状态变化与角色核心事件一致）：
  - coder/reviewer 单模式默认单轮（--once）；显式 --interval 进入常驻
  - both 默认后台化（Popen 分离会话 + 日志 runtime/logs/autoloop-both.log +
    PID runtime/locks/autoloop-both.pid，父进程立即返回）；--once 前台单轮
    （coder→reviewer 顺序，coder 失败即退出不启动 reviewer）；--foreground
    前台常驻（Ctrl-C 停止）
  - 并发唯一权威 = 角色核心脚本内层自锁（lib/autoloop_coder|reviewer.py 的
    main 锁，与旧版同路径 runtime/locks/autoloop-{coder,reviewer}.lock）；
    启动器只做非阻塞 probe 前置友好报错，probe 与执行之间竞态由内层自锁兜底。
  - both 常驻另持 autoloop-both.lock（lib/lock.py，msvcrt/fcntl 跨平台），
    进程被杀时 OS 自动释放；默认 LLM = AIOS_LLM 或 pi；--coder-llm/--reviewer-llm
    仅 both 模式分角色覆盖（TASK-079 语义：后台化/前台重入后不丢失）。
  - 修复旧 Bash 缺陷：旧版 `both --foreground` 会把 --foreground 漏传给角色
    核心导致 "未知参数"；本版 --foreground/--interval 均为启动器级标志，不进入
    角色参数。

角色核心定位：lib/autoloop_coder.py / lib/autoloop_reviewer.py（TASK-023/024），
以子进程方式运行（与旧 Bash `"$interp" "$script"` 等价），角色退出码透传。
测试钩子（不影响正常路径；隔离测试用）：
  - AUTOLOOP_LIB_DIR：覆盖角色核心目录（指向 stub 脚本目录）
  - AIOS_PROJECT_ROOT：覆盖项目根（指向临时项目目录）

纯 stdlib，零外部依赖（与 lib/config.py、lib/tasklib.py、lib/events.py 一致）。
"""
import os
import signal
import subprocess
import sys
import time

import lock  # noqa: E402  跨平台进程文件锁（TASK-012）
import tasklib  # noqa: E402  项目根定位（TASK-021）

LAUNCHER_NAME = "autoloop"
DEFAULT_BOTH_INTERVAL = 30  # both 常驻默认轮询间隔（秒），AUTOLOOP_DEFAULT_INTERVAL 可覆盖
DEFAULT_LLM = "pi"
BOTH_LOG = "autoloop-both.log"
BOTH_PID = "autoloop-both.pid"
BOTH_LOCK = "autoloop-both.lock"
STOP_TIMEOUT = 5  # stop 等待进程正常退出秒数，超时强制 kill

# Windows 控制台 GBK 无法编码 ▶ 等字符 → 统一 UTF-8 + replace（TASK-002/TASK-011 修复）。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

LAUNCHER_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg, err=False):
    """带时间戳输出（启动器层；角色核心自带 autoloop-<role>-<date>.log）。"""
    line = "[%s] %s" % (time.strftime("%F %T"), msg)
    print(line, file=sys.stderr if err else sys.stdout)


def _rel(root, path):
    """相对路径显示（跨盘时回落绝对路径，Windows os.path.relpath 会 ValueError）。"""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return os.path.abspath(path)


# ---------------- 参数解析 ----------------

class Parsed(object):
    """启动器解析结果（与旧 Bash 手工解析语义一致；rest 保留未知/透传参数原序）。"""

    def __init__(self):
        self.mode = None
        self.interval = None  # int 或 None（未传）
        self.once = False
        self.foreground = False
        self.llm = None  # str 或 None（未传）
        self.id = None
        self.timeout = None
        self.unattended = False
        self.coder_llm = None
        self.reviewer_llm = None
        self.rest = []

    def __repr__(self):
        return ("Parsed(mode=%r, interval=%r, once=%r, foreground=%r, llm=%r, "
                "id=%r, timeout=%r, unattended=%r, coder_llm=%r, reviewer_llm=%r, "
                "rest=%r)"
                % (self.mode, self.interval, self.once, self.foreground, self.llm,
                   self.id, self.timeout, self.unattended, self.coder_llm,
                   self.reviewer_llm, self.rest))


def usage():
    lines = [
        "用法: autoloop <coder|reviewer|both|stop> [--interval N] [--llm L] [--id ID] [--timeout S] [--unattended]",
        "      autoloop both [--once]                             # 默认常驻；--once 只跑单轮",
        "      autoloop both [--coder-llm L1] [--reviewer-llm L2]   # 分角色 LLM（仅 both）",
        "      autoloop both --foreground                       # 前台常驻（配合 tmux 看实时输出）",
        "      autoloop stop                                      # 停止本项目常驻 both 实例",
        "默认: both 无参数 = 常驻并自动后台（日志 runtime/logs/autoloop-both.log）；--once 前台单轮",
        "      coder/reviewer 单模式默认单轮（--once）",
        "注意: autoloop 是 Python 3 脚本（用 python kit/cli/autoloop 调用；Linux 也可 ./kit/cli/autoloop）。",
        "      cli/task 也是 Python 3 脚本，用 python kit/cli/task 调用，勿用 bash kit/cli/task",
        "      （bash 会把 Python 源码当 shell 逐行解析，产生巨量输出并死循环，脚本已自带防护）。",
    ]
    for ln in lines:
        print(ln, file=sys.stderr)
    return 1


def _take_value(argv, i, flag):
    if i + 1 >= len(argv):
        log("✗ 参数 %s 缺少值" % flag, err=True)
        return None, i
    return argv[i + 1], i + 2


def parse_args(argv):
    """解析启动器参数（首参为模式；其余按标志拆解，未知参数进 rest 透传）。"""
    if not argv:
        return None
    p = Parsed()
    p.mode = argv[0]
    i = 1
    n = len(argv)
    while i < n:
        a = argv[i]
        if a == "--interval":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            try:
                p.interval = int(v)
            except ValueError:
                log("✗ --interval 必须是整数: %s" % v, err=True)
                return None
        elif a == "--once":
            p.once = True
            i += 1
        elif a == "--foreground":
            p.foreground = True
            i += 1
        elif a == "--llm":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            p.llm = v
        elif a == "--id":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            p.id = v
        elif a == "--timeout":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            try:
                p.timeout = int(v)
            except ValueError:
                log("✗ --timeout 必须是整数: %s" % v, err=True)
                return None
        elif a == "--unattended":
            p.unattended = True
            i += 1
        elif a == "--coder-llm":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            p.coder_llm = v
        elif a == "--reviewer-llm":
            v, i = _take_value(argv, i, a)
            if v is None:
                return None
            p.reviewer_llm = v
        else:
            p.rest.append(a)
            i += 1
    return p


def build_role_args(p, role_llm=None, once=None):
    """构造单角色核心参数（与旧 Bash run_locked 默认注入一致）。

    once: True=强制 --once（both 模式每轮跑一次）；None=自动（无 --interval 时
    --once，显式 --once 优先）；False=常驻（--interval）。
    llm 优先级：分角色（--coder-llm/--reviewer-llm）> 用户 --llm > AIOS_LLM > pi。
    """
    args = []
    if once is None:
        if p.once:
            args.append("--once")
        elif p.interval is not None:
            args += ["--interval", str(p.interval)]
        else:
            args.append("--once")
    elif once:
        args.append("--once")
    else:
        args += ["--interval", str(p.interval or DEFAULT_BOTH_INTERVAL)]
    if p.id:
        args += ["--id", p.id]
    if p.timeout is not None:
        args += ["--timeout", str(p.timeout)]
    if p.unattended:
        args.append("--unattended")
    if role_llm:
        args += ["--llm", role_llm]
    elif p.llm:
        args += ["--llm", p.llm]
    else:
        args += ["--llm", os.environ.get("AIOS_LLM", DEFAULT_LLM)]
    args += p.rest
    return args


def both_interval(p):
    """both 常驻轮询间隔：--interval > AUTOLOOP_DEFAULT_INTERVAL > 30。"""
    if p.interval is not None:
        return p.interval
    try:
        return int(os.environ.get("AUTOLOOP_DEFAULT_INTERVAL", str(DEFAULT_BOTH_INTERVAL)))
    except ValueError:
        return DEFAULT_BOTH_INTERVAL


# ---------------- 角色核心调用 ----------------

def _role_lib_dir(lib_dir=None):
    """定位角色核心目录：显式参数 > AUTOLOOP_LIB_DIR（测试钩子）> 本模块目录。"""
    if lib_dir:
        return lib_dir
    return os.environ.get("AUTOLOOP_LIB_DIR") or LAUNCHER_DIR


def run_role(root, role, args, lib_dir=None):
    """运行一个角色核心（lib/autoloop_<role>.py），返回退出码。

    与旧 Bash run_locked 等价：子进程方式运行；并发唯一权威 = 角色核心内层自锁
    （锁被占用时核心打印友好提示并 exit 1）。
    """
    script = os.path.join(_role_lib_dir(lib_dir), "autoloop_%s.py" % role)
    if not os.path.isfile(script):
        log("✗ autoloop-%s 核心不存在（%s）" % (role, script), err=True)
        return 1
    log("▶ %s %s %s" % (sys.executable, os.path.basename(script), " ".join(args)))
    try:
        proc = subprocess.run([sys.executable, script] + args, cwd=root)
    except FileNotFoundError:
        log("✗ 找不到解释器: %s" % sys.executable, err=True)
        return 127
    return proc.returncode


# ---------------- both 常驻 ----------------

def _lock_probe(lockfile):
    """非阻塞探测锁：空闲返回 True（等价 lib/lock.py probe，仅友好报错用）。"""
    try:
        fd = lock._open_lock(lockfile)
    except OSError:
        return False
    held = not lock._acquire(fd)
    if not held:
        lock._release(fd)
    os.close(fd)
    return not held


def _write_pid(lock_dir, pid):
    with open(os.path.join(lock_dir, BOTH_PID), "w", encoding="ascii") as f:
        f.write(str(pid))


def _stop_handler(signum, frame):
    raise KeyboardInterrupt


def run_resident(root, p, interval, lib_dir=None):
    """both 常驻循环：持 autoloop-both.lock，写 PID，循环 {coder → reviewer}。

    前台（--foreground）与后台子进程共用此函数：后台子进程由 spawn_background
    以 `both --foreground` 分离启动，其 stdout/stderr 已重定向到
    runtime/logs/autoloop-both.log；本函数输出自然落入该日志。
    """
    lock_dir = os.path.join(root, "runtime", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    both_lock = os.path.join(lock_dir, BOTH_LOCK)
    try:
        fd = lock._open_lock(both_lock)
    except OSError as e:
        log("✗ 无法打开锁文件 %s: %s" % (both_lock, e), err=True)
        return 1
    if not lock._acquire(fd):
        log("✗ 锁已被占用: %s（拒绝启动，避免重复进程）" % both_lock, err=True)
        os.close(fd)
        return 1
    _write_pid(lock_dir, os.getpid())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop_handler)
        except (ValueError, OSError):
            pass
    if hasattr(signal, "SIGHUP"):
        try:
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    try:
        log("═══ autoloop both 常驻（PID %d，每 %ds 一轮；Ctrl-C 停止）═══"
            % (os.getpid(), interval))
        while True:
            run_role(root, "coder", build_role_args(p, p.coder_llm, once=True),
                     lib_dir=lib_dir)
            run_role(root, "reviewer", build_role_args(p, p.reviewer_llm, once=True),
                     lib_dir=lib_dir)
            log("本轮完成，%ds 后重试" % interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        log("── both 常驻停止 ──")
    finally:
        lock._release(fd)
        os.close(fd)
    return 0


def spawn_background(root, launcher_script, p, interval, lib_dir=None):
    """默认后台化：分离会话 Popen 启动常驻子进程，父进程立即返回。

    子进程以 `both --foreground ... --interval N` 重新进入本启动器，由子进程
    自己持 both 锁并写 PID（runtime/locks/autoloop-both.pid）；输出重定向到
    runtime/logs/autoloop-both.log。--coder-llm/--reviewer-llm/--llm/--id/
    --timeout/--unattended 显式加回（TASK-079 语义），AUTOLOOP_DEFAULT_INTERVAL
    经 env 继承。
    """
    lock_dir = os.path.join(root, "runtime", "locks")
    os.makedirs(lock_dir, exist_ok=True)
    both_lock = os.path.join(lock_dir, BOTH_LOCK)
    if not _lock_probe(both_lock):
        log("✗ autoloop both 已有实例在运行（%s）——拒绝启动，避免重复进程。" % both_lock,
            err=True)
        log("  等当前实例结束再试；确属残留锁可删掉该文件。", err=True)
        return 1
    log_dir = os.path.join(root, "runtime", "logs")
    os.makedirs(log_dir, exist_ok=True)
    logfile = os.path.join(log_dir, BOTH_LOG)
    child = [sys.executable, launcher_script, "both", "--foreground"]
    if p.coder_llm:
        child += ["--coder-llm", p.coder_llm]
    if p.reviewer_llm:
        child += ["--reviewer-llm", p.reviewer_llm]
    if p.llm:
        child += ["--llm", p.llm]
    if p.id:
        child += ["--id", p.id]
    if p.timeout is not None:
        child += ["--timeout", str(p.timeout)]
    if p.unattended:
        child.append("--unattended")
    child += p.rest
    child += ["--interval", str(interval)]
    try:
        f = open(logfile, "a", encoding="utf-8", errors="replace")
    except OSError as e:
        log("✗ 无法打开日志 %s: %s" % (logfile, e), err=True)
        return 1
    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                child, cwd=root, stdout=f, stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.DETACHED_PROCESS))
        else:
            proc = subprocess.Popen(child, cwd=root, stdout=f,
                                    stderr=subprocess.STDOUT,
                                    start_new_session=True)
    except OSError as e:
        log("✗ 后台启动失败: %s" % e, err=True)
        return 1
    finally:
        f.close()
    log("✓ autoloop both 已后台启动（每 %ds 一轮）" % interval)
    log("  日志: %s" % _rel(root, logfile))
    log("  停止: python %s stop" % _rel(root, launcher_script))
    return 0


# ---------------- stop ----------------

def _win_pid_alive(pid):
    """Windows 进程存活检查：OpenProcess + GetExitCodeProcess（STILL_ACTIVE=259）。"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                  False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _pid_alive(pid):
    """跨平台进程存活检查（POSIX: kill(pid, 0) 信号 0）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_pid(pid, timeout=STOP_TIMEOUT):
    """终止进程：先 SIGTERM，等待 timeout 秒，仍未退出则强制 kill（返回是否已退出）。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(1)
    try:
        os.kill(pid, 9)  # POSIX: SIGKILL；Windows: TerminateProcess（任何 sig）
    except OSError:
        pass
    return not _pid_alive(pid)


def cmd_stop(root):
    """停止本项目常驻 both 实例（PID 文件 + 锁双确认，与旧 Bash stop 一致）。"""
    lock_dir = os.path.join(root, "runtime", "locks")
    pidfile = os.path.join(lock_dir, BOTH_PID)
    if os.path.isfile(pidfile):
        try:
            with open(pidfile, encoding="ascii") as f:
                pid = int(f.read().strip() or "0")
        except (OSError, ValueError):
            pid = 0
        if pid > 0 and _pid_alive(pid):
            log("正在停止 autoloop both（PID %d）..." % pid)
            if not _kill_pid(pid):
                log("⚠ 进程 %d 未在 %ds 内正常退出，已强制 kill" % (pid, STOP_TIMEOUT), err=True)
            else:
                log("✓ autoloop both 已停止（PID %d）" % pid)
        else:
            log("· 进程 %d 已不在运行（清理过期 PID 文件）" % pid)
        try:
            os.remove(pidfile)
        except OSError:
            pass
        return 0
    both_lock = os.path.join(lock_dir, BOTH_LOCK)
    if os.path.isfile(both_lock) and not _lock_probe(both_lock):
        log("⚠ 锁被占用但无 PID 文件，无法定位进程（可能是旧版启动的实例或残留锁）。", err=True)
        log("  可用 ps aux | grep autoloop 查找并 kill 对应 PID；锁可删 %s" % both_lock, err=True)
    else:
        log("· 本项目没有运行中的 autoloop both 实例")
    return 0


# ---------------- 模式分发 ----------------

def cmd_single(root, mode, p, lib_dir=None):
    """coder/reviewer 单模式：默认单轮（--once），--interval 时转常驻。"""
    return run_role(root, mode, build_role_args(p, None, once=None), lib_dir=lib_dir)


def cmd_both(root, p, launcher_script=None, lib_dir=None):
    """both 模式：--once 前台单轮；--foreground 前台常驻；默认后台化。"""
    interval = both_interval(p)
    if p.once:
        log("═══ autoloop both（单轮）：coder（实现）→ reviewer（审查）═══")
        rc = run_role(root, "coder", build_role_args(p, p.coder_llm, once=True),
                      lib_dir=lib_dir)
        if rc != 0:
            return 1
        log("── coder 轮次结束，启动 reviewer（独立会话）──")
        run_role(root, "reviewer", build_role_args(p, p.reviewer_llm, once=True),
                 lib_dir=lib_dir)
        return 0
    if p.foreground:
        return run_resident(root, p, interval, lib_dir=lib_dir)
    return spawn_background(root, launcher_script or os.path.abspath(sys.argv[0]),
                            p, interval, lib_dir=lib_dir)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = parse_args(argv)
    if p is None:
        return usage()
    root = os.environ.get("AIOS_PROJECT_ROOT") or tasklib.find_project_root(LAUNCHER_DIR)
    if not os.path.isfile(os.path.join(root, "aios.config.yaml")):
        log("✗ 未找到项目根（aios.config.yaml），请到项目目录内运行", err=True)
        return 1
    mode = p.mode
    if mode == "stop":
        return cmd_stop(root)
    if mode in ("coder", "reviewer"):
        if p.coder_llm or p.reviewer_llm:
            log("✗ --coder-llm/--reviewer-llm 仅用于 both 模式", err=True)
            return 1
        if p.foreground:
            log("✗ --foreground 仅用于 both 模式", err=True)
            return 1
        return cmd_single(root, mode, p)
    if mode == "both":
        return cmd_both(root, p, launcher_script=os.path.abspath(sys.argv[0]))
    log("✗ 未知模式: %s（支持 coder | reviewer | both | stop）" % mode, err=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
