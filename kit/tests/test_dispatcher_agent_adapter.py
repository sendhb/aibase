"""TASK-037 — dispatcher agent 传输适配器单测。

覆盖：
- agent_adapter.LocalAdapter：语义命令 → 本地 argv（与 v1 逐字节一致）；未知命令拒绝
- agent_adapter.candidate_commands：open/in-progress/in-review 的语义序列
- agent_adapter.AgentAdapter：配置校验 / 入队（200、409 复用防双派、错误归一）/
  轮询至终态（done/failed/skipped/pickup-timeout）/ 等待超时 / 白名单拒绝 / token 只进鉴权头
- probe.fetch_aimonitor_counts / snapshot_from_aimonitor：aimonitor 聚合替身
- probe.scan_project/scan_projects：agent 条目 + fetcher 三态（aimonitor/不可达/legacy）
- registry.load_aimonitor_config：顶层段读取（含缺失/非法）
- policy.evaluate_candidates：agent 条目注入快照产生候选 + P0 fail-closed
- dispatcher：_run_candidate 语义命令序列传递；cmd_run agent 条目端到端（stub 适配器）
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

DISPATCHER_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "dispatcher"
)
sys.path.insert(0, DISPATCHER_DIR)
import agent_adapter  # noqa: E402
import downlink  # noqa: E402
import policy  # noqa: E402
import probe  # noqa: E402
import registry  # noqa: E402

from policy import Candidate  # noqa: E402


def _entry(eid="far-a", transport="agent"):
    return registry.RegistryEntry(id=eid, name=eid, path="D:/nonexistent",
                                  transport=transport)


def _cand(status="open", priority="P2", task_id="TASK-001"):
    return Candidate(entry=_entry(), task_id=task_id, status=status,
                     priority=priority, updated="2026-08-31")


class _ScriptTransport:
    """AgentAdapter transport 替身：脚本 [(method, path) -> (status, data)] 依次出队。"""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        m, p = self.steps.pop(0)
        if isinstance(m, Exception):
            raise m
        return m, p


def _adapter(transport, clock=None, sleeper=None):
    return agent_adapter.AgentAdapter(
        "http://127.0.0.1:9/", "tok", poll_interval=0.0,
        clock=clock or (lambda: 0.0), sleeper=(sleeper or (lambda s: None)),
        transport=transport)


class LocalAdapterTests(unittest.TestCase):
    def test_argv_mapping_matches_v1(self):
        captured = {}

        def fake_run(entry, command, args, timeout=1800):
            captured["command"], captured["args"] = command, list(args)
            return downlink.CommandResult(entry.id, f"{command} {' '.join(args)}",
                                          0, "", "", False)

        e = _entry("loc-a", "local")
        with mock.patch.object(agent_adapter.downlink_lib, "run", fake_run):
            for name, want in [
                ("task_start", ["kit/cli/task", "start", "TASK-001"]),
                ("autoloop_coder", ["kit/cli/autoloop-coder", "--once"]),
                ("autoloop_reviewer", ["kit/cli/autoloop-reviewer", "--once"]),
            ]:
                agent_adapter.LocalAdapter().execute(e, name, ["TASK-001"] if name == "task_start" else [])
                self.assertEqual(captured["args"], want)
                self.assertEqual(captured["command"], sys.executable)

    def test_unknown_semantic_command_rejected(self):
        with self.assertRaises(downlink.DownlinkError):
            agent_adapter.LocalAdapter().execute(_entry("l", "local"), "rm_rf", [])


class CandidateCommandsTests(unittest.TestCase):
    def test_open_sequences_task_start_then_coder(self):
        seq = agent_adapter.candidate_commands(_cand("open"))
        self.assertEqual([s[0] for s in seq], ["task_start", "autoloop_coder"])
        self.assertEqual(seq[0][1], ["TASK-001"])

    def test_inprogress_only_coder_and_review_reserved(self):
        self.assertEqual([s[0] for s in agent_adapter.candidate_commands(_cand("in-progress"))],
                         ["autoloop_coder"])
        self.assertEqual([s[0] for s in agent_adapter.candidate_commands(_cand("in-review"))],
                         ["autoloop_reviewer"])


class AgentAdapterConfigTests(unittest.TestCase):
    def test_missing_server_url_rejected(self):
        with self.assertRaises(downlink.DownlinkError):
            agent_adapter.AgentAdapter("", "tok")

    def test_missing_token_rejected(self):
        with self.assertRaises(downlink.DownlinkError):
            agent_adapter.AgentAdapter("http://x", "  ")


class AgentAdapterEnqueueTests(unittest.TestCase):
    def test_enqueue_200_returns_id_not_reused(self):
        t = _ScriptTransport([(200, {"command_id": 7})])
        cid, reused = _adapter(t).enqueue("far-a", "task_start", ["TASK-001"], 60)
        self.assertEqual((cid, reused), (7, False))
        method, path, body = t.calls[0]
        self.assertEqual((method, path), ("POST", "/api/downlink/commands"))
        self.assertEqual(body["dedup_key"], "far-a:TASK-001:task_start")
        self.assertEqual(body["command"], {"name": "task_start", "args": ["TASK-001"]})

    def test_enqueue_409_reuses_inflight_command(self):
        t = _ScriptTransport([(409, {"command_id": 9, "seq": 9, "status": "running"})])
        cid, reused = _adapter(t).enqueue("far-a", "autoloop_coder", [], 60)
        self.assertEqual((cid, reused), (9, True))
        self.assertEqual(t.calls[0][2]["dedup_key"], "far-a::autoloop_coder")

    def test_enqueue_409_without_command_id_is_error(self):
        t = _ScriptTransport([(409, {"error": "conflict"})])
        with self.assertRaises(downlink.DownlinkError):
            _adapter(t).enqueue("far-a", "autoloop_coder", [], 60)

    def test_enqueue_400_is_error(self):
        t = _ScriptTransport([(400, {"error": "bad"})])
        with self.assertRaises(downlink.DownlinkError):
            _adapter(t).enqueue("far-a", "autoloop_coder", [], 60)


class AgentAdapterExecuteTests(unittest.TestCase):
    def test_happy_path_done(self):
        t = _ScriptTransport([
            (200, {"command_id": 3}),
            (200, {"command": {"status": "queued"}}),
            (200, {"command": {"status": "running"}}),
            (200, {"command": {"status": "done",
                               "result": {"exit_code": 0,
                                          "stdout_tail": "ok-out",
                                          "stderr_tail": "ok-err"}}}),
        ])
        res = _adapter(t).execute(_entry(), "task_start", ["TASK-001"], 60)
        self.assertEqual((res.exit_code, res.stdout, res.stderr, res.timed_out),
                         (0, "ok-out", "ok-err", False))
        self.assertIn("command_id=3", res.command)
        self.assertEqual([c[:2] for c in t.calls],
                         [("POST", "/api/downlink/commands")]
                         + [("GET", "/api/downlink/commands/3")] * 3)

    def test_409_reuse_labelled(self):
        t = _ScriptTransport([
            (409, {"command_id": 5}),
            (200, {"command": {"status": "done", "result": {"exit_code": 0}}}),
        ])
        res = _adapter(t).execute(_entry(), "autoloop_coder", [], 60)
        self.assertIn("409 复用在途", res.command)
        self.assertEqual(res.exit_code, 0)

    def test_failed_exit_code_passthrough(self):
        t = _ScriptTransport([
            (200, {"command_id": 1}),
            (200, {"command": {"status": "failed",
                               "result": {"exit_code": 2, "stderr_tail": "boom"}}}),
        ])
        res = _adapter(t).execute(_entry(), "autoloop_coder", [], 60)
        self.assertEqual((res.exit_code, res.stderr, res.timed_out), (2, "boom", False))

    def test_pickup_timeout_failed_defaults_rc_1(self):
        t = _ScriptTransport([
            (200, {"command_id": 1}),
            (200, {"command": {"status": "failed",
                               "result": {"reason": "pickup-timeout", "attempts": 3}}}),
        ])
        res = _adapter(t).execute(_entry(), "autoloop_coder", [], 60)
        self.assertEqual((res.exit_code, res.timed_out, res.stdout), (1, False, ""))

    def test_skipped_is_not_failure(self):
        t = _ScriptTransport([
            (200, {"command_id": 1}),
            (200, {"command": {"status": "skipped",
                               "result": {"exit_code": 0, "reason": "already-done"}}}),
        ])
        res = _adapter(t).execute(_entry(), "task_start", ["TASK-001"], 60)
        self.assertEqual(res.exit_code, 0)

    def test_wait_timeout_reports_timed_out(self):
        clock = {"t": 0.0}

        def tick():
            clock["t"] += 100.0
            return clock["t"]

        t = _ScriptTransport([
            (200, {"command_id": 1}),
        ] + [(200, {"command": {"status": "running"}})] * 10)
        res = _adapter(t, clock=tick, sleeper=lambda s: None).execute(
            _entry(), "autoloop_coder", [], 60)
        self.assertTrue(res.timed_out)
        self.assertEqual(res.exit_code, -1)
        self.assertIn("超时", res.stderr)

    def test_whitelist_rejection_before_any_http(self):
        t = _ScriptTransport([])
        with self.assertRaises(downlink.DownlinkError):
            _adapter(t).execute(_entry(), "rm_rf", [])
        self.assertEqual(t.calls, [])

    def test_token_only_in_auth_header(self):
        t = _ScriptTransport([
            (200, {"command_id": 1}),
            (200, {"command": {"status": "done", "result": {"exit_code": 0}}}),
        ])
        res = _adapter(t).execute(_entry(), "autoloop_coder", [], 60)
        blob = json.dumps(t.calls, ensure_ascii=False)
        self.assertNotIn("tok", blob)          # token 不在 body/url/日志形状
        self.assertNotIn("tok", str(res))      # token 不进结果

    def test_auth_header_carries_token_on_http_layer(self):
        captured = []

        class _Resp:
            status = 200

            def read(self, n):
                return b'{"command_id": 11}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            captured.append(req)
            return _Resp()

        ad = agent_adapter.AgentAdapter("http://x", "secret-token", transport=None)
        with mock.patch.object(agent_adapter.urllib.request, "urlopen", fake_urlopen):
            cid, reused = ad.enqueue("far-a", "task_start", ["TASK-001"], 60)
        self.assertEqual((cid, reused), (11, False))
        req = captured[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer secret-token")
        self.assertTrue(req.full_url.startswith("http://x/api/downlink/commands"))


class ProbeAimonitorTests(unittest.TestCase):
    def test_fetch_counts_from_status_payload(self):
        payload = {"projects": [
            {"id": "far-a", "summary": {"total": 3, "open": 1, "in-progress": 1,
                                        "in-review": 0, "blocked": 0, "done": 1,
                                        "cancelled": 0}},
            {"id": "far-b", "summary": {"total": 0}},
        ]}
        counts = probe.fetch_aimonitor_counts("http://x", "far-a", http_fn=lambda u: payload)
        self.assertEqual(counts["open"], 1)
        self.assertEqual(counts["done"], 1)

    def test_fetch_counts_unreachable_or_missing(self):
        self.assertIsNone(probe.fetch_aimonitor_counts("http://x", "far-a",
                                                       http_fn=lambda u: 1 / 0))
        self.assertIsNone(probe.fetch_aimonitor_counts("http://x", "nope",
                                                       http_fn=lambda u: {"projects": []}))
        self.assertIsNone(probe.fetch_aimonitor_counts("", "far-a", http_fn=lambda u: {}))

    def test_snapshot_from_aimonitor_shape(self):
        payload = {"projects": [{"id": "far-a", "tasks": [
            {"id": "TASK-001", "slug": "TASK-001-downlink-smoke", "name": "下行烟幕",
             "status": "open", "priority": "P2", "risk": "P2", "updated": "2026-08-31"},
            {"id": "TASK-002", "slug": "TASK-002-x", "status": "done"},
        ]}]}
        snap = probe.snapshot_from_aimonitor("http://x", _entry("far-a"),
                                             http_fn=lambda u: payload)
        names = [t["name"] for t in snap["tasks"]]
        self.assertEqual(names, ["TASK-001-downlink-smoke.md", "TASK-002-x.md"])
        content = snap["tasks"][0]["content"]
        self.assertIn("status: open", content)
        self.assertIn("priority: P2", content)
        self.assertIn("approval-ref:", content)
        self.assertIn("rework-count: 0", content)

    def test_snapshot_unreachable_or_unregistered(self):
        self.assertIsNone(probe.snapshot_from_aimonitor("http://x", _entry("far-a"),
                                                        http_fn=lambda u: {"projects": []}))
        self.assertIsNone(probe.snapshot_from_aimonitor("", _entry("far-a"),
                                                        http_fn=lambda u: {}))


class ProbeScanFetcherTests(unittest.TestCase):
    def test_scan_project_agent_with_counts(self):
        e = _entry("far-a")
        res = probe.scan_project(e, status_fetcher=lambda x: {"open": 1, "in-progress": 0,
                                                              "in-review": 0, "blocked": 0,
                                                              "done": 2, "cancelled": 0})
        self.assertFalse(res["skipped"])
        self.assertEqual(res["source"], "aimonitor")
        self.assertEqual(res["counts"]["done"], 2)

    def test_scan_project_agent_unreachable(self):
        res = probe.scan_project(_entry("far-a"), status_fetcher=lambda x: None)
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "aimonitor-unreachable")

    def test_scan_project_agent_legacy_without_fetcher(self):
        res = probe.scan_project(_entry("far-a"), status_fetcher=None)
        self.assertTrue(res["skipped"])
        self.assertEqual(res["reason"], "agent-transport")

    def test_scan_project_local_has_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = registry.RegistryEntry(id="loc-a", name="loc-a", path=tmp, transport="local")
            res = probe.scan_project(e)
            self.assertFalse(res["skipped"])
            self.assertEqual(res["source"], "local")


class RegistryAimonitorConfigTests(unittest.TestCase):
    def _write(self, data):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, f)
        f.close()
        return f.name

    def test_reads_top_level_section(self):
        p = self._write({"projects": [], "aimonitor": {"server_url": "http://10.0.0.2:9000/"}})
        try:
            self.assertEqual(registry.load_aimonitor_config(p)["server_url"],
                             "http://10.0.0.2:9000")  # 尾部 / 归一
        finally:
            os.unlink(p)

    def test_missing_or_invalid_section_returns_none(self):
        p = self._write({"projects": []})
        try:
            self.assertIsNone(registry.load_aimonitor_config(p)["server_url"])
        finally:
            os.unlink(p)
        self.assertIsNone(registry.load_aimonitor_config("D:/definitely-missing.json")["server_url"])
        p2 = self._write({"projects": [], "aimonitor": {"server_url": "   "}})
        try:
            self.assertIsNone(registry.load_aimonitor_config(p2)["server_url"])
        finally:
            os.unlink(p2)


class PolicyAgentSnapshotTests(unittest.TestCase):
    def test_agent_entry_with_snapshot_yields_candidate(self):
        snap = {"tasks": [{"name": "TASK-001-agent-test.md",
                           "content": "---\nname: t\nmetadata:\n  status: open\n"
                                      "  priority: P2\n  risk: P2\n  approval-ref: none\n"
                                      "  rework-count: 0\n---\n"}]}
        considered = policy.evaluate_candidates([_entry("far-a")], max_workers=1,
                                                snapshots={"far-a": snap})
        self.assertEqual(len(considered), 1)
        self.assertEqual((considered[0].entry.id, considered[0].task_id,
                          considered[0].decision), ("far-a", "TASK-001", "ok"))

    def test_agent_entry_without_snapshot_skipped(self):
        self.assertEqual(policy.evaluate_candidates([_entry("far-a")], max_workers=1), [])

    def test_agent_p0_without_approval_ref_fail_closed(self):
        snap = {"tasks": [{"name": "TASK-002-agent-test.md",
                           "content": "---\nname: t\nmetadata:\n  status: open\n"
                                      "  priority: P0\n  risk: P0\n  approval-ref: \n"
                                      "  rework-count: 0\n---\n"}]}
        considered = policy.evaluate_candidates([_entry("far-a")], max_workers=1,
                                                snapshots={"far-a": snap})
        self.assertEqual(len(considered), 1)
        self.assertEqual(considered[0].decision, "p0-blocked")


class DispatcherRunAgentEntryTests(unittest.TestCase):
    def test_run_candidate_passes_semantic_commands_to_adapter(self):
        seq = agent_adapter.candidate_commands(_cand("open"))
        calls = []

        class Stub:
            def execute(self, entry, name, args, timeout=1800):
                calls.append((name, list(args), timeout))
                return downlink.CommandResult(entry.id, name, 0, "", "", False)

        import dispatcher as dispatcher_cli
        results = dispatcher_cli._run_candidate(_entry(), _cand("open"), timeout=77,
                                                adapter=Stub())
        self.assertEqual([r[1].exit_code for r in results], [0, 0])
        self.assertEqual(calls, [(s[0], s[1], 77) for s in seq])

    def test_adapter_for_local_and_agent(self):
        import dispatcher as dispatcher_cli
        self.assertIsInstance(dispatcher_cli._adapter_for(_entry("l", "local"), None),
                              agent_adapter.LocalAdapter)
        with mock.patch.dict(os.environ, {"AIOS_DOWNLINK_TOKEN": "t"}):
            ad = dispatcher_cli._adapter_for(_entry("far-a"), "http://x")
            self.assertIsInstance(ad, agent_adapter.AgentAdapter)
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(downlink.DownlinkError):
                dispatcher_cli._adapter_for(_entry("far-a"), "http://x")

    def test_cmd_run_agent_entry_end_to_end(self):
        import dispatcher as dispatcher_cli
        snap = {"tasks": [{"name": "TASK-001-agent-e2e.md",
                           "content": "---\nname: t\nmetadata:\n  status: open\n"
                                      "  priority: P2\n  risk: P2\n  approval-ref: none\n"
                                      "  rework-count: 0\n---\n"}]}
        args = types.SimpleNamespace(max_workers=1, config=None, dry_run=False,
                                     state_dir=None, timeout=60)
        executed = []

        class StubAdapter:
            def execute(self, entry, name, cmd_args, timeout=1800):
                executed.append((entry.id, name, list(cmd_args), timeout))
                return downlink.CommandResult(entry.id, name, 0, "", "", False)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = os.path.join(tmp, "projects.json")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"projects": [{"id": "far-a", "path": "D:/x", "transport": "agent"}],
                           "aimonitor": {"server_url": "http://127.0.0.1:9"}},
                          f)
            args.config = cfg
            out = io.StringIO()
            with (
                mock.patch.object(dispatcher_cli.probe_lib, "snapshot_from_aimonitor",
                                  return_value=snap),
                mock.patch.object(dispatcher_cli.agent_adapter_lib, "AgentAdapter",
                                  lambda *a, **k: StubAdapter()),
                mock.patch.dict(os.environ, {"AIOS_DOWNLINK_TOKEN": "t"}),
                mock.patch.object(sys, "stdout", out),
            ):
                rc = dispatcher_cli.cmd_run(args, [_entry("far-a")])
        self.assertEqual(rc, 0)
        self.assertEqual([e[1] for e in executed], ["task_start", "autoloop_coder"])
        self.assertEqual(executed[0][2], ["TASK-001"])
        self.assertIn("far-a TASK-001", out.getvalue())


if __name__ == "__main__":
    unittest.main()
