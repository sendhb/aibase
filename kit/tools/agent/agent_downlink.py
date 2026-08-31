"""agent_downlink.py — 下行指令拾取/校验/执行/回报（TASK-036，AGENT-DOWNLINK-CONTRACT v1.0）。

双套独立校验（契约 §六，agent 侧第二道闸，与 server 不共享代码路径）：
- 命令白名单：仅 task_start / autoloop_coder / autoloop_reviewer（独立枚举，不 import server）
- 项目白名单：command.project_id 必须 ∈ agent.json projects（server per-token 白名单之外的独立闸）

执行模型（R2-001 并发模型，契约 §三）：
- 拾取循环独立于执行：执行在 worker 线程，主循环每轮 tick() 不被长任务阻塞，
  ingest 心跳照常——「忙而非死」不误判 stale。
- 执行期间跳过拾取调用（busy）：server 回收是惰性（仅 pickup 调用时扫描），
  queued 指令的 pickup 超时窗口实际暂停；领取即 running，执行期只有 timeout_secs 生效。
- 执行超时自报（SMELL-004 agent 侧）：子进程超时 kill → 回报 failed(exit_code=-1)。

安全（Rule of Two，契约 §六）：①不可信输入+③修改状态=允许组合；②敏感数据不入通道——
回报前脱敏（剔除 Authorization/Bearer/token 行）+ ≤200 行 tail，密钥绝不入回报/日志。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import agent_http

DOWNLINK_PICKUP_PATH = "/api/downlink/pickup"
DOWNLINK_RESULT_PATH = "/api/downlink/commands/{command_id}/result"

# 命令白名单（契约 §二；agent 侧独立枚举——与 server 各自维护，防一处被改两处失守）
COMMAND_WHITELIST = ("task_start", "autoloop_coder", "autoloop_reviewer")
COMMAND_ARGV = {
    "task_start": ["kit/cli/task", "start"],
    "autoloop_coder": ["kit/cli/autoloop", "coder", "--once"],
    "autoloop_reviewer": ["kit/cli/autoloop", "reviewer", "--once"],
}
DEFAULT_TIMEOUT_SECS = 1800
TAIL_MAX_LINES = 200  # 契约 §四：stdout/stderr tail ≤200 行
SECRET_LINE_RE = re.compile(r"authorization|bearer|token", re.IGNORECASE)
TASK_REF_RE = re.compile(r"^TASK-\d+$")
SKIPPED_TASK_STATUSES = ("done", "in-review")  # 契约 §三 幂等三层②


class CommandRejected(Exception):
    """agent 侧第二道闸拒绝（命令白名单外 / 项目白名单外 / schema 非法）。"""


def validate_command(command, projects):
    """agent 侧第二道闸：校验通过返回项目条目 {id, path}；拒绝抛 CommandRejected。"""
    if not isinstance(command, dict):
        raise CommandRejected("指令对象缺失或非法")
    inner = command.get("command")
    if not isinstance(inner, dict):
        raise CommandRejected("command 字段缺失或非法")
    name = inner.get("name")
    if name not in COMMAND_WHITELIST:
        raise CommandRejected(f"命令白名单外: {name!r}（允许: {', '.join(COMMAND_WHITELIST)}）")
    args = inner.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise CommandRejected("command.args 必须是字符串数组")
    pid = command.get("project_id")
    for proj in projects or []:
        if proj.get("id") == pid:
            return proj
    raise CommandRejected(f"项目白名单外: {pid!r}（agent.json projects 无此项目）")


def build_argv(name, args, python=sys.executable):
    """白名单命令 → 本地 CLI argv（契约 §二映射；python 用 agent 自身解释器）。"""
    return [python] + COMMAND_ARGV[name] + list(args)


def scrub_tail(text):
    """回报前脱敏（契约 §四）：剔除含 Authorization/Bearer/token 字样的行 + ≤200 行。"""
    lines = [line for line in (text or "").splitlines() if not SECRET_LINE_RE.search(line)]
    return "\n".join(lines[-TAIL_MAX_LINES:])


def read_task_status(project_path, task_ref):
    """读项目本地任务状态（幂等预检用）；文件缺失/解析不出 → None（交由 CLI 自然失败）。"""
    tasks_dir = os.path.join(project_path, "runtime", "tasks")
    try:
        names = sorted(os.listdir(tasks_dir))
    except OSError:
        return None
    for fname in names:
        if fname.startswith(task_ref + "-") or fname == task_ref + ".md":
            try:
                with open(os.path.join(tasks_dir, fname), encoding="utf-8") as f:
                    head = f.read(600)
            except OSError:
                return None
            m = re.search(r"^\s*status:\s*(\S+)", head, re.MULTILINE)
            return m.group(1) if m else None
    return None


def _report(status, exit_code, stdout_tail, stderr_tail):
    return {"status": status, "exit_code": exit_code,
            "stdout_tail": scrub_tail(stdout_tail), "stderr_tail": scrub_tail(stderr_tail),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def execute_command(command, project, python=sys.executable, runner=None):
    """执行一条指令 → 回报 dict（契约 §四 schema）。

    - 幂等预检：args[0] 为 TASK-xxx 且本地任务已 done/in-review → skipped（三层②）。
    - 执行超时（timeout_secs）→ 子进程 kill → failed(exit_code=-1)（契约 §五/SMELL-004）。
    """
    if runner is None:
        runner = subprocess.run
    name = command["command"]["name"]
    args = command["command"]["args"]
    if args and TASK_REF_RE.match(args[0]):
        status = read_task_status(project["path"], args[0])
        if status in SKIPPED_TASK_STATUSES:
            return _report("skipped", 0, f"precheck: 任务已 {status}，跳过执行", "")
    argv = build_argv(name, args, python)
    timeout = command.get("timeout_secs") or DEFAULT_TIMEOUT_SECS
    try:
        proc = runner(argv, cwd=project["path"], capture_output=True,
                      text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return _report("failed", -1, "", f"execution timeout after {timeout}s（子进程已 kill）")
    except OSError as e:
        return _report("failed", -1, "", f"spawn failed: {e}")
    status = "done" if proc.returncode == 0 else "failed"
    return _report(status, proc.returncode, proc.stdout or "", proc.stderr or "")


def pickup_command(server_url, token, connect_timeout=agent_http.CONNECT_TIMEOUT,
                   read_timeout=agent_http.READ_TIMEOUT):
    """GET /api/downlink/pickup（契约 §三）→ 指令 dict | None；HTTP 复用 agent_http 基建。

    401/4xx → PushRejectedError，5xx → PushServerError，网络 → PushNetworkError（可重试）。
    """
    req = urllib.request.Request(server_url.rstrip("/") + DOWNLINK_PICKUP_PATH, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("User-Agent", agent_http.USER_AGENT)
    opener = agent_http._build_opener(connect_timeout, read_timeout)
    try:
        with opener.open(req) as resp:
            body = json.loads(agent_http._read_body(resp))  # _read_body 已 UTF-8 解码为 str
    except urllib.error.HTTPError as e:
        try:
            resp_body = agent_http._read_body(e)
        except (urllib.error.URLError, TimeoutError, OSError):
            resp_body = ""
        finally:
            e.close()
        agent_http._raise_for_status(e.code, resp_body, e.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise agent_http.PushNetworkError(str(e)) from e
    return body.get("command") if isinstance(body, dict) else None


def report_result(server_url, token, command_id, report,
                  connect_timeout=agent_http.CONNECT_TIMEOUT,
                  read_timeout=agent_http.READ_TIMEOUT):
    """POST /api/downlink/commands/{id}/result（契约 §四）。

    已终态（409）→ 幂等忽略，返回 "already-terminal"；成功返回 server 响应 dict。
    """
    url = server_url.rstrip("/") + DOWNLINK_RESULT_PATH.format(command_id=command_id)
    body = json.dumps(report, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", agent_http.USER_AGENT)
    opener = agent_http._build_opener(connect_timeout, read_timeout)
    try:
        with opener.open(req) as resp:
            return json.loads(agent_http._read_body(resp))
    except urllib.error.HTTPError as e:
        try:
            resp_body = agent_http._read_body(e)
        except (urllib.error.URLError, TimeoutError, OSError):
            resp_body = ""
        finally:
            e.close()
        if e.code == 409:
            return "already-terminal"  # 已终态 → 幂等忽略（契约 §五，非错误）
        agent_http._raise_for_status(e.code, resp_body, e.headers)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise agent_http.PushNetworkError(str(e)) from e


class DownlinkWorker:
    """单槽下行 worker：主循环每轮 tick()；执行在独立线程（R2-001）。

    tick() 返回 "busy"（执行中，跳过拾取）/ "empty"（无可领）/ "dispatched"（已提交执行）/
    "rejected"（第二道闸拒绝，已回报 failed）。执行/回报失败仅记日志，不抛出主循环。
    """

    def __init__(self, cfg, log, pickup_fn=pickup_command, execute_fn=execute_command,
                 report_fn=report_result, spawner=None):
        self.cfg = cfg
        self.log = log
        self._pickup = pickup_fn
        self._execute = execute_fn
        self._report = report_fn
        self._spawner = spawner or (lambda target: threading.Thread(target=target, daemon=True))
        self._lock = threading.Lock()
        self._busy = False

    def tick(self):
        if not self._lock.acquire(blocking=False):
            return "busy"  # 槽被短暂占用（worker 置位中）——下一轮再拾取，不阻塞主循环
        try:
            if self._busy:
                return "busy"
            self._busy = True  # 拾取→提交全程持单槽，防两线程并发执行
        finally:
            self._lock.release()
        try:
            command = self._pickup(self.cfg["server_url"], self.cfg["token"])
        except agent_http.PushError as e:
            with self._lock:
                self._busy = False
            self.log.error(f"downlink 拾取失败（下轮重试）: {e}")
            return "pickup-error"
        if not command:
            with self._lock:
                self._busy = False
            return "empty"
        try:
            project = validate_command(command, self.cfg["projects"])
        except CommandRejected as e:
            self._finish_rejected(command, e)
            return "rejected"
        thread = self._spawner(lambda: self._run(command, project))
        thread.start()
        return "dispatched"

    def wait_idle(self, timeout=None, sleep=time.sleep, clock=time.time):
        """等待 worker 槽空闲（--once 单轮退出前调用，防 daemon 执行线程被进程退出截杀）。

        执行线程内有子进程 timeout 兜底，正常必然返回；timeout 超时返回 False。
        """
        deadline = None if timeout is None else clock() + timeout
        while True:
            if self._lock.acquire(blocking=False):
                busy = self._busy
                self._lock.release()
                if not busy:
                    return True
            if deadline is not None and clock() >= deadline:
                return False
            sleep(0.05)

    def _finish_rejected(self, command, error):
        """第二道闸拒绝 → 回报 failed（指令已 running，不回报则挂死 → SMELL-004）。"""
        cid = command.get("command_id")
        self.log.error(f"downlink 拒绝 command_id={cid}: {error}")
        try:
            self._report(self.cfg["server_url"], self.cfg["token"], cid,
                         _report("failed", -1, "", f"agent-side rejected: {error}"))
        except Exception as e:  # 回报失败绝不卡死单槽（SMELL-004 兜底）
            self.log.error(f"downlink 拒绝回报失败 command_id={cid}: {e!r}")
        finally:
            with self._lock:
                self._busy = False

    def _run(self, command, project):
        cid = command.get("command_id")
        try:
            try:
                report = self._execute(command, project)
            except Exception as e:  # 意外异常 → 转 failed 回报，绝不滞留 running（SMELL-004）
                report = _report("failed", -1, "", f"agent internal error: {e!r}")
            self.log.info(f"downlink command_id={cid} 执行完成: {report['status']}")
            try:
                outcome = self._report(self.cfg["server_url"], self.cfg["token"], cid, report)
                if outcome == "already-terminal":
                    self.log.info(f"downlink command_id={cid} 已终态（409 幂等忽略）")
            except Exception as e:  # 回报失败仅记日志（指令滞留 running，服务端清扫属后续运维项）
                self.log.error(f"downlink 回报失败 command_id={cid}: {e!r}")
        finally:
            with self._lock:
                self._busy = False
