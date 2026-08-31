"""agent_adapter.py — 下行传输适配器（TASK-037，PHASE3-V2-CROSSMACHINE-DESIGN §五-4）。

适配器接口（A2A 挂载点，设计稿 §六：未来 A2AAdapter 实现同一接口，调用方零改动）：

    adapter.execute(entry, name, args, timeout) → downlink.CommandResult（与本地同形）

语义命令白名单（AGENT-DOWNLINK-CONTRACT §二，dispatcher 侧语义层）：
    task_start / autoloop_coder / autoloop_reviewer

- LocalAdapter：本地子进程（复用 downlink.run，argv 由语义命令映射；与 v1 逐字节一致）
- AgentAdapter：指令经 aimonitor 指令队列——POST /api/downlink/commands 入队
  （dedup_key 幂等；409 = 同 key 在途 → 复用既有指令防双派）→ 轮询
  GET /api/downlink/commands/{id} 至终态 → CommandResult。状态语义与本地一致：
  allocate→running（入队即记）→ done/failed；等待超时 timed_out（stale 由 state.py 回收）。

安全（Rule of Two / 设计稿 §四）：dispatcher token 只经 Authorization 头传递，
不入注册表/日志/代码（token 从环境变量 AIOS_DOWNLINK_TOKEN 读取，CLI 层负责）。
零外部依赖（仅 stdlib）。
"""
import json
import shlex
import sys
import time
import urllib.error
import urllib.request

from downlink import CommandResult, DownlinkError  # noqa: E402,F401
import downlink as downlink_lib  # noqa: E402

# 语义命令 → 本地 argv（与 v1 _run_candidate 逐字节一致，含 autoloop-coder/-reviewer shim）
LOCAL_ARGV = {
    "task_start": ["kit/cli/task", "start"],
    "autoloop_coder": ["kit/cli/autoloop-coder", "--once"],
    "autoloop_reviewer": ["kit/cli/autoloop-reviewer", "--once"],
}
# agent 通道白名单名（契约 §二；与 agent 侧各自独立枚举，防一处被改两处失守）
AGENT_NAMES = ("task_start", "autoloop_coder", "autoloop_reviewer")
POLL_INTERVAL_DEFAULT = 5.0   # 指令状态轮询间隔（秒；远小于 agent poll_interval）
ACK_MARGIN = 90.0             # 拾取余量（秒；覆盖 agent 轮询周期 + 网络抖动）
ACK_MARGIN_ENV = "AIOS_DOWNLINK_ACK_MARGIN"  # 可环境变量覆盖（集成验证/烟幕用短值）


class LocalAdapter:
    """本地子进程适配器：复用 downlink.run，行为与 v1 逐字节一致。"""

    name = "local"

    def execute(self, entry, name, args, timeout=1800):
        if name not in LOCAL_ARGV:
            raise DownlinkError(f"未知语义命令（本地）: {name!r}")
        # 晚绑定 downlink_lib.run（模块属性查找）：单测补丁点与 v1 一致
        return downlink_lib.run(entry, sys.executable,
                                LOCAL_ARGV[name] + list(args or []), timeout=timeout)


class AgentAdapter:
    """agent 消息通道适配器：指令经 aimonitor 队列下行（TASK-037）。"""

    name = "agent"

    def __init__(self, server_url, token, poll_interval=POLL_INTERVAL_DEFAULT,
                 clock=time.time, sleeper=time.sleep, transport=None,
                 ack_margin=ACK_MARGIN):
        if not server_url or not str(server_url).strip():
            raise DownlinkError(
                "agent 通道缺 server_url（注册表顶层 aimonitor.server_url）")
        if not token or not str(token).strip():
            raise DownlinkError(
                "agent 通道缺 token（环境变量 AIOS_DOWNLINK_TOKEN；token 不入注册表/日志）")
        self.server_url = str(server_url).strip().rstrip("/")
        self.token = str(token)
        self.poll_interval = poll_interval
        self.ack_margin = float(ack_margin)
        self._clock = clock
        self._sleep = sleeper
        self._transport = transport or self._http_json

    # ---- HTTP 层（stdlib urllib；错误归一为 DownlinkError，token 只进鉴权头）----

    def _http_json(self, method, path, body=None):
        req = urllib.request.Request(
            self.server_url + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None,
            method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(1 << 20).decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                raw = e.read(1 << 20).decode("utf-8", errors="replace")
            except OSError:
                raw = ""
            finally:
                e.close()
            try:
                return e.code, (json.loads(raw) if raw else None)
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise DownlinkError(f"aimonitor 不可达（{self.server_url}）: {ex}") from ex

    # ---- 指令生命周期（契约 §二/§三/§五）----

    def enqueue(self, entry_id, name, args, timeout):
        """入队；返回 (command_id, reused)。409（同 dedup_key 在途）→ 复用防双派。"""
        dedup = f"{entry_id}:{(args[0] if args else '')}:{name}"
        body = {"project_id": entry_id, "dedup_key": dedup,
                "command": {"name": name, "args": list(args or [])},
                "timeout_secs": int(timeout)}
        status, data = self._transport("POST", "/api/downlink/commands", body)
        if status == 200 and isinstance(data, dict) and data.get("command_id") is not None:
            return data["command_id"], False
        if status == 409 and isinstance(data, dict) and data.get("command_id") is not None:
            return data["command_id"], True
        raise DownlinkError(f"指令入队失败（{entry_id}）: HTTP {status} {data}")

    def poll(self, command_id):
        """轮询单条指令状态（GET /api/downlink/commands/{id}）→ command dict。"""
        status, data = self._transport("GET", f"/api/downlink/commands/{command_id}")
        if status != 200 or not isinstance(data, dict):
            raise DownlinkError(f"指令状态轮询失败: HTTP {status}")
        return data.get("command") or {}

    def execute(self, entry, name, args, timeout=1800):
        """语义命令 → 入队 → 轮询至终态/超时 → CommandResult（与本地同形）。

        - 白名单外命令拒绝（dispatcher 侧第一道闸语义对齐，契约 §二）；
        - 等待上限 = timeout + ACK_MARGIN（覆盖拾取周期）；超时 timed_out=True、
          exit_code=-1（stale 回收交由 state.py task-timeout 机制，语义与本地一致）；
        - 终态映射：done→rc=result.exit_code；skipped→rc（预检跳过非失败）；
          failed→rc（pickup-timeout 无 exit_code → 1）。
        """
        if name not in AGENT_NAMES:
            raise DownlinkError(
                f"语义命令不在 agent 白名单（契约 §二: {', '.join(AGENT_NAMES)}）: {name!r}")
        command_id, reused = self.enqueue(entry.id, name, args, timeout)
        label = f"agent channel command_id={command_id}" + ("（409 复用在途）" if reused else "")
        deadline = self._clock() + timeout + self.ack_margin
        while True:
            row = self.poll(command_id)
            st = row.get("status")
            if st in ("done", "failed", "skipped"):
                result = row.get("result") or {}
                default_rc = 0 if st == "done" else 1
                try:
                    rc = int(result.get("exit_code", default_rc))
                except (TypeError, ValueError):
                    rc = default_rc
                return CommandResult(
                    entry_id=entry.id, command=label, exit_code=rc,
                    stdout=result.get("stdout_tail") or "",
                    stderr=result.get("stderr_tail") or "",
                    timed_out=False)
            if self._clock() >= deadline:
                return CommandResult(
                    entry_id=entry.id, command=label, exit_code=-1,
                    stdout="",
                    stderr=f"下行等待超时（{timeout}s+{self.ack_margin:g}s 余量）: 最后状态 {st}",
                    timed_out=True)
            self._sleep(self.poll_interval)


def candidate_commands(candidate):
    """候选 → 语义命令序列 [(name, args, label)]（传输无关；从 _run_candidate 提取）。

    - open：先 task_start（本地 CLI 落盘状态），再 autoloop_coder（实现+verify）；
    - in-progress：autoloop_coder；
    - in-review → autoloop_reviewer 是【保留分支】：v1 policy 只选 open/in-progress，
      当前不会选到 in-review 候选，仅留给未来 policy 扩展。
    """
    cmds = []
    if candidate.status == "open":
        cmds.append(("task_start", [candidate.task_id],
                     f"task start {candidate.task_id}"))
    if candidate.status == "in-review":
        cmds.append(("autoloop_reviewer", [],
                     "autoloop-reviewer --once（保留分支，v1 不触发）"))
    else:
        cmds.append(("autoloop_coder", [], "autoloop-coder --once"))
    return cmds
