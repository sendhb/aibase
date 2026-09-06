"""TASK-104 — session 日志流 agent 端单测（消息级增量 + 字节偏移游标 + 体积护栏）。

覆盖验收标准（aibase 侧 / agent 端）：
- 标准1：向 runtime/logs/sessions/ 写 N 行 → 一轮推送（stub HTTP 捕获 payload）
  payload 含新增行；第二轮推送游标前进、无重复行
- 标准2：session 目录缺失/为空 → payload 不含 sessions 字段且不报错
- 标准6：单批 session 增量超上限 → 截断并带 truncated 标记，游标只反映已送行
- 读取层：缺失/空 → None、增量读取、半行不纳入、文件截断重建归零重读、
  游标读写原子性与非法输入拒绝
- payload 层：sessions=None 不含键（向后兼容 v1.0）、非法类型 PayloadError
- 循环层：_incremental_sessions 预算截批 / 单行截断 / task 分组 / 心跳优先
  （预算不足时 session 让位，快照/事件照常推送）
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "tools", "telemetry"
)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_loop  # noqa: E402
import agent_payload  # noqa: E402
import agent_runtime  # noqa: E402


class FakePusher:
    """推送替身：记录调用；errors 按序弹出抛错，空则成功。"""

    def __init__(self, errors=None):
        self.errors = list(errors or [])
        self.calls = []

    def __call__(self, server_url, token, body):
        self.calls.append({"server_url": server_url, "token": token, "body": body})
        if self.errors:
            raise self.errors.pop(0)


def make_project(lines_by_task=None):
    """构造临时被监控项目：runtime/logs/sessions/<TASK-ID>/<file>.jsonl。

    lines_by_task: {task_id: {filename: [line, ...]}}；返回 (project_dir, sessions_dir)
    """
    root = tempfile.mkdtemp(prefix="task104-")
    sessions = os.path.join(root, "runtime", "logs", "sessions")
    for task_id, files in (lines_by_task or {}).items():
        for fn, lines in files.items():
            d = os.path.join(sessions, task_id)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in lines))
    return root, sessions


def write_lines(path, lines):
    with open(path, "a", encoding="utf-8") as f:
        f.write("".join(line + "\n" for line in lines))


def poll(tmp, pusher=None, **overrides):
    """对临时项目跑一轮 poll_once（真实读取/构造/序列化，注入推送替身）。"""
    cfg = {
        "server_url": "http://127.0.0.1:1/api/ingest",
        "token": "t",
        "projects": [{"id": "proj-1", "path": tmp}],
        "poll_interval_seconds": 30,
    }
    pusher = pusher or FakePusher()
    kwargs = dict(states={}, push_fn=pusher)
    kwargs.update(overrides)
    pushed, skipped, failed = agent_loop.poll_once(cfg, **kwargs)
    return pushed, skipped, failed, pusher


def payload_bodies(pusher):
    return [json.loads(c["body"]) for c in pusher.calls]


def session_lines_of(payload):
    """从 payload 提取 sessions.items 的全部行（展平）。"""
    part = payload.get("sessions")
    if part is None:
        return None
    out = []
    for item in part["items"]:
        for f in item["files"]:
            out.extend(f["lines"])
    return out


def agent_http_push_error():
    """构造 agent_http.PushError（不引入真实网络依赖）。"""
    import agent_http
    return agent_http.PushError("boom")


class SessionCursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp, _ = make_project()
        os.makedirs(os.path.join(self.tmp, "runtime", "logs"), exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_or_corrupt_cursor_returns_none(self):
        self.assertIsNone(agent_runtime.read_session_push_cursor(self.tmp))
        logs = os.path.join(self.tmp, "runtime", "logs")
        with open(os.path.join(logs, agent_runtime.SESSION_CURSOR_FILE), "w") as f:
            f.write("not json{")
        self.assertIsNone(agent_runtime.read_session_push_cursor(self.tmp))

    def test_invalid_entries_rejected_whole(self):
        logs = os.path.join(self.tmp, "runtime", "logs")
        bad = [
            {"offsets": {"a/f.jsonl": -1}},
            {"offsets": {"a/f.jsonl": True}},
            {"offsets": {"a/f.jsonl": "12"}},
            {"offsets": {"": 1}},
            {"offsets": [1, 2]},
            [1, 2],
        ]
        for data in bad:
            with open(os.path.join(logs, agent_runtime.SESSION_CURSOR_FILE), "w") as f:
                json.dump(data, f)
            self.assertIsNone(agent_runtime.read_session_push_cursor(self.tmp), data)

    def test_write_read_roundtrip_and_validation(self):
        offsets = {"TASK-1/a.jsonl": 123, "TASK-2/b.jsonl": 0}
        agent_runtime.write_session_push_cursor(self.tmp, offsets)
        self.assertEqual(agent_runtime.read_session_push_cursor(self.tmp), offsets)
        # 原子写：无 .tmp 残留
        logs = os.path.join(self.tmp, "runtime", "logs")
        self.assertFalse(os.path.exists(
            os.path.join(logs, agent_runtime.SESSION_CURSOR_FILE + ".tmp")))
        for bad in ([], "x", {"k": -1}, {"k": True}, {"k": 1.5}, {"": 1}, {1: 2}):
            with self.assertRaises(ValueError):
                agent_runtime.write_session_push_cursor(self.tmp, bad)


class ReadSessionDeltasTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.sessions = make_project()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_dir_or_no_jsonl_returns_none(self):
        self.assertIsNone(agent_runtime.read_session_deltas(self.tmp))
        os.makedirs(os.path.join(self.sessions, "TASK-1"))
        with open(os.path.join(self.sessions, "TASK-1", "notes.txt"), "w") as f:
            f.write("x")
        self.assertIsNone(agent_runtime.read_session_deltas(self.tmp))  # 只有非 jsonl

    def test_first_read_full_then_incremental_no_overlap(self):
        f1 = os.path.join(self.sessions, "TASK-1")
        os.makedirs(f1)
        path = os.path.join(f1, "s.jsonl")
        lines = ['{"n":1}', '{"n":2}', '{"n":3}']
        write_lines(path, lines)
        deltas, offsets = agent_runtime.read_session_deltas(self.tmp)
        self.assertEqual(len(deltas), 1)
        d = deltas[0]
        self.assertEqual((d["task_id"], d["name"], d["key"], d["start"]),
                         ("TASK-1", "s.jsonl", "TASK-1/s.jsonl", 0))
        self.assertEqual([r["text"] for r in d["lines"]], lines)
        # end = 累计字节（含换行）
        self.assertEqual(d["lines"][-1]["end"],
                         sum(len(l.encode()) + 1 for l in lines))
        self.assertEqual(offsets, {"TASK-1/s.jsonl": d["lines"][-1]["end"]})
        # 无新增 → 空列表 + 偏移保持
        deltas, offsets2 = agent_runtime.read_session_deltas(self.tmp, offsets)
        self.assertEqual(deltas, [])
        self.assertEqual(offsets2, offsets)
        # 追加 → 仅新增行，游标前进，无重复
        more = ['{"n":4}', '{"n":5}']
        write_lines(path, more)
        deltas, offsets3 = agent_runtime.read_session_deltas(self.tmp, offsets)
        self.assertEqual([r["text"] for r in deltas[0]["lines"]], more)
        self.assertEqual(deltas[0]["start"], offsets["TASK-1/s.jsonl"])
        self.assertGreater(offsets3["TASK-1/s.jsonl"],
                           offsets["TASK-1/s.jsonl"])

    def test_partial_trailing_line_excluded(self):
        d = os.path.join(self.sessions, "TASK-1")
        os.makedirs(d)
        path = os.path.join(d, "s.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a":1}\n{"partial")')  # 第二行无换行
        deltas, offsets = agent_runtime.read_session_deltas(self.tmp)
        self.assertEqual([r["text"] for r in deltas[0]["lines"]], ['{"a":1}'])
        self.assertEqual(offsets["TASK-1/s.jsonl"], 8)
        # 半行补全换行后，下轮可读到完整行
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n")
        deltas, offsets = agent_runtime.read_session_deltas(self.tmp, offsets)
        self.assertEqual([r["text"] for r in deltas[0]["lines"]], ['{"partial")'])

    def test_truncated_or_recreated_file_rereads_from_zero(self):
        d = os.path.join(self.sessions, "TASK-1")
        os.makedirs(d)
        path = os.path.join(d, "s.jsonl")
        write_lines(path, ['{"a":1}', '{"a":2}', '{"a":3}'])
        _, offsets = agent_runtime.read_session_deltas(self.tmp)
        self.assertGreater(offsets["TASK-1/s.jsonl"], 0)
        # 文件被重建得更短（游标 > size）→ 归零全量重读
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"new":1}\n')
        deltas, offsets2 = agent_runtime.read_session_deltas(self.tmp, offsets)
        self.assertEqual(deltas[0]["start"], 0)
        self.assertEqual([r["text"] for r in deltas[0]["lines"]], ['{"new":1}'])

    def test_multibyte_line_offsets_are_byte_based(self):
        d = os.path.join(self.sessions, "TASK-1")
        os.makedirs(d)
        path = os.path.join(d, "s.jsonl")
        lines = ['{"zh":"中文思考"}', '{"n":2}']
        write_lines(path, lines)
        deltas, offsets = agent_runtime.read_session_deltas(self.tmp)
        expected_end = sum(len(l.encode("utf-8")) + 1 for l in lines)
        self.assertEqual(deltas[0]["lines"][-1]["end"], expected_end)
        self.assertEqual(offsets["TASK-1/s.jsonl"], expected_end)

    def test_multiple_tasks_and_files_sorted(self):
        for tid, files in {"TASK-2": {"b.jsonl": ['{"t2b":1}'], "a.jsonl": ['{"t2a":1}']},
                           "TASK-1": {"c.jsonl": ['{"t1":1}']}}.items():
            for fn, lines in files.items():
                dd = os.path.join(self.sessions, tid)
                os.makedirs(dd, exist_ok=True)
                write_lines(os.path.join(dd, fn), lines)
        deltas, offsets = agent_runtime.read_session_deltas(self.tmp)
        self.assertEqual([d["key"] for d in deltas],
                         ["TASK-1/c.jsonl", "TASK-2/a.jsonl", "TASK-2/b.jsonl"])
        self.assertEqual(len(offsets), 3)


class BuildPayloadSessionsTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"tasks": None, "focus": None, "heartbeats": [],
                         "events": [], "verification_count": None,
                         "review_count": None}

    def test_none_omits_key_backward_compatible(self):
        p = agent_payload.build_payload("proj", self.snapshot)
        self.assertNotIn("sessions", p)
        self.assertNotIn("events", p)
        self.assertNotIn("cursor", p)

    def test_valid_sessions_written(self):
        part = {"items": [{"task_id": "T", "files": [{"name": "s.jsonl", "lines": ["x"]}]}],
                "truncated": True, "cursor": {"T/s.jsonl": 5}}
        p = agent_payload.build_payload("proj", self.snapshot, sessions=part)
        self.assertEqual(p["sessions"]["truncated"], True)
        self.assertEqual(p["sessions"]["cursor"], {"T/s.jsonl": 5})
        self.assertEqual(p["sessions"]["items"][0]["task_id"], "T")

    def test_invalid_sessions_raise(self):
        bad = [
            ["not", "a", "dict"],
            {"truncated": False, "cursor": {}},                       # 缺 items
            {"items": [], "cursor": {}},                              # 缺 truncated
            {"items": [], "truncated": "yes", "cursor": {}},          # truncated 非法
            {"items": [], "truncated": False},                        # 缺 cursor
            {"items": [], "truncated": False, "cursor": {"k": -1}},   # 负偏移
            {"items": [], "truncated": False, "cursor": {"": 1}},     # 空键
            {"items": [], "truncated": False, "cursor": {"k": True}},
        ]
        for part in bad:
            with self.assertRaises(agent_payload.PayloadError, msg=part):
                agent_payload.build_payload("proj", self.snapshot, sessions=part)


class IncrementalSessionsTests(unittest.TestCase):
    @staticmethod
    def delta(task_id, name, rows, start=0):
        return {"task_id": task_id, "name": name,
                "key": f"{task_id}/{name}", "start": start, "lines": rows}

    def test_none_and_empty(self):
        self.assertEqual(agent_loop._incremental_sessions(None, None), (None, None))
        part, cur = agent_loop._incremental_sessions([], {"T/f.jsonl": 10})
        self.assertEqual(part, {"items": [], "truncated": False,
                                "cursor": {"T/f.jsonl": 10}})
        self.assertEqual(cur, {"T/f.jsonl": 10})

    def test_budget_truncation_no_loss_and_next_round_continues(self):
        rows = [{"text": f'{{"n":{i}}}', "end": 10 * (i + 1)} for i in range(6)]
        deltas = [self.delta("T", "f.jsonl", rows)]
        # 预算只够 2 行（每行成本 = len(json)+1 ≈ 10）
        part, cur = agent_loop._incremental_sessions(deltas, None, max_bytes=25)
        self.assertTrue(part["truncated"])
        self.assertEqual(part["items"][0]["files"][0]["lines"],
                         ['{"n":0}', '{"n":1}'])
        self.assertEqual(cur, {"T/f.jsonl": 20})  # 游标只到已送行末
        # 第二轮：新预算送剩余 4 行
        part2, cur2 = agent_loop._incremental_sessions(
            [self.delta("T", "f.jsonl", rows[2:], start=20)], cur, max_bytes=1000)
        self.assertFalse(part2["truncated"])
        self.assertEqual([r for r in part2["items"][0]["files"][0]["lines"]],
                         ['{"n":2}', '{"n":3}', '{"n":4}', '{"n":5}'])
        self.assertEqual(cur2, {"T/f.jsonl": 60})

    def test_lines_never_lost_when_batch_truncated(self):
        # 回归：截批时已装入行必须随批送达（游标推进 ⇔ 行在 payload 中）
        rows = [{"text": f'{{"n":{i}}}', "end": 10 * (i + 1)} for i in range(4)]
        part, cur = agent_loop._incremental_sessions(
            [self.delta("T", "f.jsonl", rows)], None, max_bytes=25)
        sent = part["items"][0]["files"][0]["lines"]
        self.assertEqual(len(sent), 2)
        self.assertEqual(cur["T/f.jsonl"], 20)
        self.assertEqual(max(int(json.loads(s)["n"]) for s in sent), 1)

    def test_oversized_line_truncated_with_suffix(self):
        big = "x" * (agent_payload.MAX_SESSION_LINE_BYTES + 100)
        rows = [{"text": big, "end": 10 ** 6}]
        part, cur = agent_loop._incremental_sessions(
            [self.delta("T", "f.jsonl", rows)], None,
            max_bytes=agent_payload.MAX_SESSION_BYTES)
        self.assertFalse(part["truncated"])
        line = part["items"][0]["files"][0]["lines"][0]
        self.assertTrue(line.endswith(agent_payload.TRUNCATION_SUFFIX))
        self.assertLessEqual(len(line.encode("utf-8")),
                             agent_payload.MAX_SESSION_LINE_BYTES + 16)
        self.assertEqual(cur, {"T/f.jsonl": 10 ** 6})  # 游标推过原行，不重推

    def test_grouping_and_deterministic_order(self):
        deltas = [
            self.delta("TASK-2", "b.jsonl", [{"text": "b1", "end": 3}]),
            self.delta("TASK-1", "c.jsonl", [{"text": "c1", "end": 3}]),
            self.delta("TASK-2", "a.jsonl", [{"text": "a1", "end": 3}]),
        ]
        part, _ = agent_loop._incremental_sessions(deltas, None, max_bytes=10 ** 6)
        self.assertEqual([i["task_id"] for i in part["items"]], ["TASK-1", "TASK-2"])
        self.assertEqual([f["name"] for f in part["items"][1]["files"]],
                         ["a.jsonl", "b.jsonl"])

    def test_unaffordable_file_does_not_block_later_files(self):
        deltas = [
            self.delta("T-big", "f.jsonl",
                       [{"text": "y" * 500, "end": 501}]),
            self.delta("T-small", "g.jsonl", [{"text": "ok", "end": 3}]),
        ]
        part, cur = agent_loop._incremental_sessions(deltas, None, max_bytes=100)
        self.assertTrue(part["truncated"])
        self.assertEqual([i["task_id"] for i in part["items"]], ["T-small"])
        self.assertNotIn("T-big/f.jsonl", cur)
        self.assertEqual(cur.get("T-small/g.jsonl"), 3)


class PollOnceSessionIntegrationTests(unittest.TestCase):
    """验收标准 1/2/6 的端到端（agent 侧）单测：stub HTTP 捕获 payload。"""

    def setUp(self):
        self.tmp, self.sessions = make_project()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_criterion1_push_delta_then_cursor_advances_no_duplicates(self):
        task_dir = os.path.join(self.sessions, "TASK-104")
        os.makedirs(task_dir)
        path = os.path.join(task_dir, "s.jsonl")
        first = [f'{{"i":{i}}}' for i in range(5)]
        write_lines(path, first)
        pusher = FakePusher()
        states = {}
        pushed, _, failed = agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states=states, push_fn=pusher)
        self.assertEqual((pushed, failed), (1, 0))
        bodies = payload_bodies(pusher)
        self.assertEqual(session_lines_of(bodies[0]), first)
        cursor_file = os.path.join(self.tmp, "runtime", "logs",
                                   agent_runtime.SESSION_CURSOR_FILE)
        cur1 = agent_runtime.read_session_push_cursor(self.tmp)
        self.assertEqual(cur1, {"TASK-104/s.jsonl":
                                sum(len(l) + 1 for l in first)})
        # 第二轮：追加 3 行 → 仅新增行、游标前进、无重复
        more = [f'{{"i":{i}}}' for i in range(5, 8)]
        write_lines(path, more)
        pushed, _, failed = agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states=states, push_fn=pusher)
        self.assertEqual((pushed, failed), (1, 0))
        bodies = payload_bodies(pusher)
        second_lines = session_lines_of(bodies[1])
        self.assertEqual(second_lines, more)
        self.assertFalse(set(first) & set(second_lines))
        cur2 = agent_runtime.read_session_push_cursor(self.tmp)
        self.assertGreater(cur2["TASK-104/s.jsonl"], cur1["TASK-104/s.jsonl"])
        # 第三轮：无新增 → 空确认（items=[]、truncated=False）
        agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states=states, push_fn=pusher)
        bodies = payload_bodies(pusher)
        self.assertEqual(bodies[2]["sessions"]["items"], [])
        self.assertFalse(bodies[2]["sessions"]["truncated"])

    def test_criterion2_missing_or_empty_sessions_omits_key(self):
        # 目录缺失
        pusher = FakePusher()
        pushed, _, failed = agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states={}, push_fn=pusher)
        self.assertEqual((pushed, failed), (1, 0))
        self.assertNotIn("sessions", payload_bodies(pusher)[0])
        # 目录存在但为空（无任何 jsonl）
        os.makedirs(os.path.join(self.sessions, "TASK-X"))
        pusher2 = FakePusher()
        agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states={}, push_fn=pusher2)
        self.assertNotIn("sessions", payload_bodies(pusher2)[0])

    def test_push_failure_does_not_advance_session_cursor(self):
        task_dir = os.path.join(self.sessions, "TASK-1")
        os.makedirs(task_dir)
        write_lines(os.path.join(task_dir, "s.jsonl"), ['{"a":1}'])
        pusher = FakePusher(errors=[agent_http_push_error()])
        pushed, _, failed = agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states={}, push_fn=pusher)
        self.assertEqual((pushed, failed), (0, 1))
        self.assertIsNone(agent_runtime.read_session_push_cursor(self.tmp))
        # 成功后游标落盘
        pushed, _, failed = agent_loop.poll_once(
            {"server_url": "u", "token": "t",
             "projects": [{"id": "p", "path": self.tmp}]},
            states={}, push_fn=FakePusher())
        self.assertEqual(pushed, 1)
        cur = agent_runtime.read_session_push_cursor(self.tmp)
        self.assertEqual(cur, {"TASK-1/s.jsonl": 8})

    def test_criterion6_size_guard_truncates_with_flag(self):
        task_dir = os.path.join(self.sessions, "TASK-1")
        os.makedirs(task_dir)
        path = os.path.join(task_dir, "s.jsonl")
        lines = [f'{{"i":{i},"pad":"{"p" * 30}"}}' for i in range(12)]
        write_lines(path, lines)
        states = {}
        bodies = []
        with patch.object(agent_payload, "MAX_SESSION_BYTES", 300), \
             patch.object(agent_payload, "MIN_SESSION_BUDGET", 64):
            for _ in range(3):
                pusher = FakePusher()
                agent_loop.poll_once(
                    {"server_url": "u", "token": "t",
                     "projects": [{"id": "p", "path": self.tmp}]},
                    states=states, push_fn=pusher)
                bodies.extend(payload_bodies(pusher))
        # 第一轮截断带标记；三轮并集 = 全部行、无重复；游标不越过已送行
        self.assertTrue(bodies[0]["sessions"]["truncated"])
        all_sent = []
        for b in bodies:
            lines_sent = session_lines_of(b) or []
            self.assertFalse(set(all_sent) & set(lines_sent))
            all_sent.extend(lines_sent)
        self.assertEqual(sorted(all_sent), sorted(lines))
        cur = agent_runtime.read_session_push_cursor(self.tmp)
        self.assertEqual(cur["TASK-1/s.jsonl"],
                         sum(len(l.encode()) + 1 for l in lines))

    def test_heartbeat_priority_session_skipped_when_budget_starved(self):
        task_dir = os.path.join(self.sessions, "TASK-1")
        os.makedirs(task_dir)
        write_lines(os.path.join(task_dir, "s.jsonl"), ['{"a":1}'])
        pusher = FakePusher()
        with patch.object(agent_payload, "MAX_PAYLOAD_BYTES", 1024), \
             patch.object(agent_payload, "SESSION_ACCOUNT_MARGIN", 512):
            pushed, _, failed = agent_loop.poll_once(
                {"server_url": "u", "token": "t",
                 "projects": [{"id": "p", "path": self.tmp}]},
                states={}, push_fn=pusher)
        # 快照+事件余量不足 MIN_SESSION_BUDGET → 本轮 session 让位，心跳照发
        self.assertEqual((pushed, failed), (1, 0))
        self.assertNotIn("sessions", payload_bodies(pusher)[0])
        self.assertIsNone(agent_runtime.read_session_push_cursor(self.tmp))


if __name__ == "__main__":
    unittest.main()
