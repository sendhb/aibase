"""TASK-027 — kit/tools/agent/ 主循环与常驻单测。

覆盖：
- poll_once 一轮：全部成功 / 单项目失败（可重试与不可重试）→ 退避计数 /
  退避中项目本轮跳过 / 项目间隔离（A 失败退避不阻塞 B）/ payload 构造失败与
  PayloadTooLargeError → 记失败不推送
- run_forever 常驻：按 interval 轮询、睡眠按 POLL_SLEEP_STEP 分片、
  睡眠中停止标志置位 → 当前轮完成后不再 poll（干净退出）、启动前已置位 → 零 poll、
  interval 非法 → ValueError
- AgentLog：quiet 抑制 info 保留 error；默认两者都输出
- StopFlag + 信号：request_stop 置位并记录 signum；install_signal_handlers 注册
  SIGINT/SIGTERM 置位标志（测试后恢复处理器，不污染测试进程）
- CLI 子进程 E2E：--once + 本地 mock ingest 成功（收到 1 个请求、Authorization 正确）、
  --once --quiet 连接失败（exit 0 + 错误在 stderr + stdout 无常规日志）、
  --interval 非法 → exit 2、常驻 SIGTERM → exit 0 无 traceback
"""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "agent"
)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 复用 test_agent_http 的 mock 服务器
import agent_loop  # noqa: E402
import agent_http  # noqa: E402
import agent_payload  # noqa: E402
from test_agent_http import MockIngestServer  # noqa: E402


class FakeClock:
    """可手动推进的假时钟（time_fn / clock 注入用），替代真实时间。"""

    def __init__(self, start=0.0):
        self.t = float(start)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class FakeSleeper:
    """睡眠替身：记录请求秒数并推进假时钟（不真实 sleep）。"""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.advance(seconds)


class FakePusher:
    """推送替身：记录调用；errors 序列按调用顺序依次弹出抛错，空则成功。"""

    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.calls = []

    def __call__(self, server_url, token, body):
        self.calls.append({"server_url": server_url, "token": token, "body": body})
        if self.errors:
            raise self.errors.pop(0)


def ok_cfg():
    """两个项目的规范化配置；server_url 指向不可达端口 1（单测注入推送替身，不会真连）。"""
    return {
        "server_url": "http://127.0.0.1:1/api/ingest",
        "token": "secret-token",
        "projects": [
            {"id": "proj-a", "path": "/nonexistent/a"},
            {"id": "proj-b", "path": "/nonexistent/b"},
        ],
        "poll_interval_seconds": 30,
    }


class PollOnceTests(unittest.TestCase):
    def test_all_success(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (2, 0, 0))
        self.assertEqual(len(pusher.calls), 2)
        for call, pid in zip(pusher.calls, ("proj-a", "proj-b")):
            self.assertEqual(call["server_url"], ok_cfg()["server_url"])
            self.assertEqual(call["token"], "secret-token")
            body = json.loads(call["body"])
            self.assertEqual(body["project_id"], pid)
            self.assertIsNone(body["files"]["tasks"])  # runtime 缺失容错 → payload 正常
        for pid in ("proj-a", "proj-b"):
            self.assertEqual(states[pid].consecutive_failures, 0)
            self.assertTrue(states[pid].can_push())  # 成功未进入退避

    def test_single_failure_backoff_and_isolation(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher(errors=[agent_http.PushServerError(500, body="boom")])
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 0, 1))
        self.assertEqual(states["proj-a"].consecutive_failures, 1)
        self.assertFalse(states["proj-a"].can_push())  # A 退避中
        self.assertEqual(states["proj-a"].next_retry_at, 1001.0)  # 失败后退避 1s（now + 1）
        self.assertEqual(states["proj-b"].consecutive_failures, 0)
        self.assertTrue(states["proj-b"].can_push())  # B 不受影响

    def test_non_retryable_failure_still_backs_off(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher(errors=[agent_http.PushRejectedError(401)])
        states = {}
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 0, 1))
        self.assertEqual(states["proj-a"].consecutive_failures, 1)
        self.assertFalse(states["proj-a"].can_push())

    def test_backoff_project_skipped(self):
        clock = FakeClock(1000.0)
        states = {}
        # 第一轮：proj-a 失败进入退避（next_retry_at = 1001）
        agent_loop.poll_once(ok_cfg(), states=states, clock=clock,
                             push_fn=FakePusher(errors=[agent_http.PushServerError(503)]))
        self.assertFalse(states["proj-a"].can_push())
        # 第二轮：proj-a 跳过，proj-b 正常推送
        pusher = FakePusher()
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (1, 1, 0))
        self.assertEqual(len(pusher.calls), 1)
        self.assertEqual(json.loads(pusher.calls[0]["body"])["project_id"], "proj-b")

    def test_backoff_expires_then_pushes(self):
        clock = FakeClock(1000.0)
        states = {}
        agent_loop.poll_once(ok_cfg(), states=states, clock=clock,
                             push_fn=FakePusher(errors=[agent_http.PushNetworkError("boom")]))
        self.assertFalse(states["proj-a"].can_push())
        # 时间推进超过退避 → 恢复推送
        clock.advance(1.0)
        pusher = FakePusher()
        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (2, 0, 0))
        self.assertEqual(states["proj-a"].consecutive_failures, 0)  # 成功清零

    def test_payload_error_records_failure_no_push(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}

        def bad_payload(pid, snapshot):
            raise agent_payload.PayloadError("模拟构造失败")

        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, payload_fn=bad_payload, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (0, 0, 2))
        self.assertEqual(pusher.calls, [])
        self.assertEqual(states["proj-a"].consecutive_failures, 1)

    def test_payload_too_large_records_failure_no_push(self):
        clock = FakeClock(1000.0)
        pusher = FakePusher()
        states = {}

        def bad_serialize(payload):
            raise agent_payload.PayloadTooLargeError("模拟超大 payload")

        pushed, skipped, failed = agent_loop.poll_once(
            ok_cfg(), states=states, clock=clock, serialize_fn=bad_serialize, push_fn=pusher)
        self.assertEqual((pushed, skipped, failed), (0, 0, 2))
        self.assertEqual(pusher.calls, [])


class RunForeverTests(unittest.TestCase):
    def _run(self, interval, stop_fn, clock, sleeper, poller):
        return agent_loop.run_forever(
            ok_cfg(), interval, log=agent_loop.AgentLog(quiet=True),
            stop_fn=stop_fn, clock=clock, sleeper=sleeper, poller=poller)

    def test_polls_at_interval_and_chunks_sleep(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        def stop_fn():
            return len(poll_counts) >= 2

        result = self._run(2.0, stop_fn, clock, sleeper, poller)
        self.assertEqual(result, "stopped")
        self.assertEqual(poll_counts, [0.0, 2.0])  # 间隔 2s 轮询
        self.assertEqual(sleeper.calls, [0.5, 0.5, 0.5, 0.5])  # 2s 按 0.5s 分片

    def test_stop_during_sleep_exits_after_current_round(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []
        stop = agent_loop.StopFlag()

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        # 睡眠 3 个分片后（t=1.5）请求停止 → 第 2 轮不应发生
        def stop_fn():
            if clock() >= 1.5:
                stop.request_stop(signal.SIGINT)
            return stop()

        self._run(2.0, stop_fn, clock, sleeper, poller)
        self.assertEqual(poll_counts, [0.0])
        self.assertEqual(sleeper.calls, [0.5, 0.5, 0.5])

    def test_stop_before_start_no_poll(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        poll_counts = []
        stop = agent_loop.StopFlag()
        stop.request_stop(signal.SIGTERM)

        def poller(cfg, states=None, log=None, clock=None):
            poll_counts.append(clock())

        result = self._run(1.0, stop, clock, sleeper, poller)
        self.assertEqual(result, "stopped")
        self.assertEqual(poll_counts, [])
        self.assertEqual(sleeper.calls, [])

    def test_invalid_interval_rejected(self):
        clock = FakeClock(0.0)
        sleeper = FakeSleeper(clock)
        for bad in (0, -1, "5", True, None):
            with self.assertRaises(ValueError, msg=f"interval={bad!r}"):
                self._run(bad, lambda: True, clock, sleeper, lambda **kw: None)


class AgentLogTests(unittest.TestCase):
    def test_quiet_suppresses_info_keeps_error(self):
        out, err = io.StringIO(), io.StringIO()
        log = agent_loop.AgentLog(quiet=True, stream=out, err_stream=err)
        log.info("常规")
        log.error("故障")
        self.assertEqual(out.getvalue(), "")
        self.assertIn("故障", err.getvalue())

    def test_default_outputs_both(self):
        out, err = io.StringIO(), io.StringIO()
        log = agent_loop.AgentLog(quiet=False, stream=out, err_stream=err)
        log.info("常规")
        log.error("故障")
        self.assertIn("常规", out.getvalue())
        self.assertIn("故障", err.getvalue())


class StopFlagTests(unittest.TestCase):
    def test_stop_flag_callable_and_signum(self):
        flag = agent_loop.StopFlag()
        self.assertFalse(flag())
        flag.request_stop(signal.SIGTERM)
        self.assertTrue(flag())
        self.assertEqual(flag.signum, signal.SIGTERM)
        # 首个信号被记录，后续请求不覆盖
        flag.request_stop(signal.SIGINT)
        self.assertEqual(flag.signum, signal.SIGTERM)

    def test_install_signal_handlers_sets_flag(self):
        flag = agent_loop.StopFlag()
        installed = agent_loop.install_signal_handlers(flag)
        self.assertIn(signal.SIGINT, installed)
        self.assertIn(signal.SIGTERM, installed)
        try:
            os.kill(os.getpid(), signal.SIGINT)
            deadline = time.time() + 2.0
            while not flag.stopped and time.time() < deadline:
                time.sleep(0.01)
        finally:
            for sig in installed:  # 恢复默认处理器，不污染测试进程
                signal.signal(sig, signal.SIG_DFL)
        self.assertTrue(flag.stopped)
        self.assertEqual(flag.signum, signal.SIGINT)


def _write_config(tmp, server_url, project_path, poll=30):
    cfg = {
        "server_url": server_url,
        "token": "secret-token",
        "projects": [{"id": "proj-e2e", "path": project_path}],
        "poll_interval_seconds": poll,
    }
    path = os.path.join(tmp, "agent.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


class AgentCliOnceTests(unittest.TestCase):
    """子进程 E2E：--once 推送一轮（真实本地 mock ingest）。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def setUp(self):
        # 防止环境 http_proxy 劫持 127.0.0.1 本地 mock（同 test_agent_http）
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    def test_once_pushes_one_round(self):
        with tempfile.TemporaryDirectory() as tmp, \
                MockIngestServer(response=(200, "ok", {})) as server:
            cfg_path = _write_config(tmp, server.ingest_url(), tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--once", "--config", cfg_path],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(server.requests), 1)  # 单轮 = 1 个请求
            req = server.requests[0]
            self.assertEqual(req["path"], "/api/ingest")
            self.assertEqual(req["headers"].get("Authorization"), "Bearer secret-token")
            body = json.loads(req["body"])
            self.assertEqual(body["project_id"], "proj-e2e")
            self.assertIn("单轮结束", proc.stdout)

    def test_once_quiet_failure_error_visible_exit_0(self):
        # 指向不可达端口 1：轮内推送失败（可重试网络错误）→ exit 0，错误在 stderr
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--once", "--quiet", "--config", cfg_path],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("推送失败", proc.stderr)
            self.assertEqual(proc.stdout, "")  # quiet 抑制所有常规日志

    def test_interval_invalid_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            for bad in ("0", "-5", "abc"):
                proc = subprocess.run(
                    [sys.executable, self.AGENT, "--interval", bad,
                     "--check-config", "--config", cfg_path],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(proc.returncode, 2, f"interval={bad}")
                self.assertIn("interval", proc.stderr)

    def test_interval_valid_with_check_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = _write_config(tmp, "http://127.0.0.1:1/api/ingest", tmp)
            proc = subprocess.run(
                [sys.executable, self.AGENT, "--interval", "10",
                 "--check-config", "--config", cfg_path],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("配置 OK", proc.stdout)


class AgentCliDaemonSignalTests(unittest.TestCase):
    """子进程 E2E：常驻模式收到 SIGTERM 干净退出（exit 0、无 traceback）。"""

    AGENT = os.path.join(AGENT_DIR, "agent.py")

    def setUp(self):
        self._old_no_proxy = os.environ.get("no_proxy")
        os.environ["no_proxy"] = "127.0.0.1,localhost"

    def tearDown(self):
        if self._old_no_proxy is None:
            os.environ.pop("no_proxy", None)
        else:
            os.environ["no_proxy"] = self._old_no_proxy

    def test_sigterm_clean_exit(self):
        with tempfile.TemporaryDirectory() as tmp, \
                MockIngestServer(response=(200, "ok", {})) as server:
            cfg_path = _write_config(tmp, server.ingest_url(), tmp, poll=30)
            proc = subprocess.Popen(
                [sys.executable, self.AGENT, "--config", cfg_path, "--quiet"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                # 等待第一轮推送到达 mock 服务器（启动即 poll 一轮）
                deadline = time.time() + 15
                while time.time() < deadline:
                    if len(server.requests) >= 1:
                        break
                    time.sleep(0.05)
                self.assertGreaterEqual(len(server.requests), 1, "第一轮推送未到达")
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertNotIn("KeyboardInterrupt", stderr)


if __name__ == "__main__":
    unittest.main()
