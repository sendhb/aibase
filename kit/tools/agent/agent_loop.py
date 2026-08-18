"""agent_loop.py — 主循环与常驻（kit/tools/agent/ 循环层）。

汇聚 TASK-022 配置 / TASK-023 runtime 读取 / TASK-024 payload 构造 /
TASK-025 HTTP 推送 / TASK-026 重试退避，组成常驻轮询主循环：

    python3 agent.py                     # 常驻：按 poll_interval_seconds（默认 30s）轮询
    python3 agent.py --once              # 单轮：推送一轮后退出（cron / systemd timer）
    python3 agent.py --interval 10       # 覆盖轮询间隔（秒）
    python3 agent.py --quiet             # 安静模式：抑制常规日志，错误始终输出 stderr

设计要点：
- 每项目独立退避状态（PushRetryState per project）：A 项目失败退避不阻塞 B 项目推送；
  退避中的项目本轮跳过（can_push 语义，TASK-026），轮询间隔恒定、不 sleep 累积。
- 信号：SIGINT/SIGTERM 注册处理器 → 置位 StopFlag（处理器内不抛异常，在途 HTTP 请求
  自然完成）；常驻睡眠按 POLL_SLEEP_STEP 分片——PEP 475 下 signal 不中断 time.sleep，
  分片保证信号响应延迟上限 ≈ 0.5s + 在途 read_timeout（≤30s），随后干净退出。
- 可测试：时钟/睡眠/读取/构造/序列化/推送函数全部可注入，单测零真实网络与 sleep。
"""
import math
import signal
import sys
import time

import agent_http
import agent_payload
import agent_retry
import agent_runtime

POLL_SLEEP_STEP = 0.5  # 常驻睡眠分片（秒）：信号响应延迟上限（不含在途 HTTP 请求）


class AgentLog:
    """agent 日志器：quiet=True 时常规日志（info）静默，错误（error）始终输出。

    info → stdout，error → stderr：故障/退避告警必须可见，不随 --quiet 隐藏。
    """

    def __init__(self, quiet=False, stream=None, err_stream=None):
        self.quiet = bool(quiet)
        self.stream = stream if stream is not None else sys.stdout
        self.err_stream = err_stream if err_stream is not None else sys.stderr

    def info(self, message):
        if not self.quiet:
            print(f"agent: {message}", file=self.stream)

    def error(self, message):
        print(f"agent: {message}", file=self.err_stream)


class StopFlag:
    """可调用停止标志：信号处理器置位，主循环每轮/每个睡眠分片后检查。

    作为 stop_fn 传入 run_forever（callable）；signum 记录首个触发信号，
    供退出日志显示 SIGINT/SIGTERM。
    """

    def __init__(self):
        self.stopped = False
        self.signum = None

    def __call__(self):
        return self.stopped

    def request_stop(self, signum=None):
        if signum is not None and self.signum is None:
            self.signum = signum
        self.stopped = True


def _make_signal_handler(flag, sig):
    def handler(signum, frame):
        flag.request_stop(signum)
    return handler


def install_signal_handlers(flag):
    """注册 SIGINT/SIGTERM → 置位 stop 标志；返回成功注册的信号列表。

    非主线程（ValueError）或环境不允许（OSError）时跳过该信号而不抛异常——
    单测可在任意线程安全调用。处理器只置位标志不抛异常，保证"干净退出"。
    """
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _make_signal_handler(flag, sig))
            installed.append(sig)
        except (ValueError, OSError):
            continue
    return installed


def poll_once(cfg, states=None, log=None, clock=time.time,
              read_fn=agent_runtime.read_project_runtime,
              payload_fn=agent_payload.build_payload,
              serialize_fn=agent_payload.serialize_payload,
              push_fn=agent_http.push_payload):
    """执行一轮轮询：遍历 cfg["projects"] 逐项目 读取→构造→序列化→推送。

    参数:
        cfg:      agent_config.validate 规范化配置（server_url/token/projects）
        states:   {project_id: PushRetryState} 跨轮共享状态表；缺省新建空表
        log:      AgentLog；缺省新建（quiet=False）
        clock:    时间来源（time_fn 注入，FakeClock 供单测）
        其余 *fn: 读取/构造/序列化/推送函数，单测注入替身（默认接真实模块）
    返回:
        (pushed, skipped, failed)：成功推送 / 退避跳过 / 失败的项目数
    """
    if states is None:
        states = {}
    if log is None:
        log = AgentLog()

    pushed = skipped = failed = 0
    for project in cfg["projects"]:
        pid = project["id"]
        state = states.get(pid)
        if state is None:
            state = states[pid] = agent_retry.PushRetryState(time_fn=clock)

        if not state.can_push():
            skipped += 1
            log.info(f"{pid}: 退避中（约 {state.seconds_until_retry():.0f}s 后重试），本轮跳过")
            continue

        try:
            snapshot = read_fn(project["path"])
            payload = payload_fn(pid, snapshot)
            body = serialize_fn(payload)
        except agent_payload.PayloadError as e:
            failed += 1
            state.record_failure()
            log.error(f"{pid}: payload 构造失败（已记录退避）: {e}")
            continue

        try:
            push_fn(cfg["server_url"], cfg["token"], body)
        except agent_http.PushError as e:
            failed += 1
            retry_after = getattr(e, "retry_after", None)
            delay = state.record_failure(retry_after=retry_after)
            if agent_http.is_retryable(e):
                log.error(f"{pid}: 推送失败（可重试），约 {delay:.0f}s 后重试: {e}")
            else:
                log.error(f"{pid}: 推送失败（不可重试，已记录退避）: {e}")
            continue

        state.record_success()
        pushed += 1
        log.info(f"{pid}: 推送成功")
    return pushed, skipped, failed


def _positive_seconds(name, value):
    """校验正有限秒数并返回 float；非法抛 ValueError（run_forever 防御性入参校验）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是正数（秒）")
    fv = float(value)
    if not math.isfinite(fv) or fv <= 0:
        raise ValueError(f"{name} 必须是正数（秒）")
    return fv


def run_forever(cfg, interval, states=None, log=None, stop_fn=None,
                clock=time.time, sleeper=time.sleep, poller=poll_once):
    """常驻循环：每轮 poll_once 后按 interval 睡眠（分片），stop_fn 置位时干净退出。

    参数:
        cfg:      规范化配置（agent_config.validate 输出）
        interval: 轮询间隔（秒，正数；config 或 --interval，CLI 层保证已校验）
        states:   跨轮共享退避状态表（缺省新建）
        log:      AgentLog（缺省新建）
        stop_fn:  停止判定 callable（缺省永不停止）；信号处理器置位的 StopFlag
        clock:    时间来源（单测注入 FakeClock）
        sleeper:  睡眠函数（单测注入手动推进时钟的替身）
        poller:   单轮执行函数（缺省 poll_once；单测注入计数替身）
    返回:
        "stopped"（退出原因；供测试/日志断言）
    """
    interval = _positive_seconds("interval", interval)
    if stop_fn is None:
        stop_fn = lambda: False
    if states is None:
        states = {}
    if log is None:
        log = AgentLog()

    log.info(f"常驻启动：{len(cfg['projects'])} 个项目，轮询间隔 {interval:g}s"
             f"（SIGINT/SIGTERM 干净退出）")
    while not stop_fn():
        poller(cfg, states=states, log=log, clock=clock)
        deadline = clock() + interval
        while not stop_fn():
            remaining = deadline - clock()
            if remaining <= 0.0:
                break
            sleeper(min(remaining, POLL_SLEEP_STEP))

    signum = getattr(stop_fn, "signum", None)
    if signum is None:
        log.info("停止标志置位，干净退出")
    else:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        log.info(f"收到信号 {sig_name}，干净退出")
    return "stopped"
