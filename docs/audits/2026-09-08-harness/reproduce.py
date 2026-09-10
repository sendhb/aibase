#!/usr/bin/env python3
"""Independent audit probes. Only temporary fixtures; never invoke a real LLM.

Run from any directory:
  python3 -B docs/audits/2026-09-08-harness/reproduce.py
Outputs observations, not a certification or a production regression suite.
"""
import ast
import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
MONITOR = ROOT.parent / "aimonitor"
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "kit/cli/lib"),
                str(ROOT / "kit/tools/dispatcher"),
                str(ROOT / "kit/tools/telemetry")]
import autoloop_coder as coder
import autoloop_launcher as launcher
import autoloop_reviewer as reviewer
import tasklib
import llm
import policy
import registry
import state
import dispatcher
import agent_adapter
import agent_downlink
import agent_payload
import agent_runtime
import context_loader


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


bridge = load("audit_mcp", ROOT / "kit/tools/mcp/server.py")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def task(root, number=1, status="open", risk="P2", priority="P2",
         deps="[]", assignee="human", reviewer_id="any", extra=""):
    name = f"TASK-{number:03d}-audit"
    path = root / "runtime/tasks" / (name + ".md")
    write(path, f"---\nname: {name}\ndescription: audit fixture\nmetadata:\n"
          f"  type: task\n  status: {status}\n  priority: {priority}\n"
          f"  risk: {risk}\n  approval-ref: none\n  assignee: {assignee}\n"
          f"  reviewer: {reviewer_id}\n  rework-count: 0\n  depends-on: {deps}\n"
          f"  created: {tasklib.today()}\n  updated: {tasklib.today()}\n"
          f"{extra}---\n\n- [x] fixture acceptance\n")
    return path


def project(root):
    write(root / "aios.config.yaml", "version: 1\nsource_dirs:\n  - src/\n"
          "generated_dirs:\n  - dist/\ncommands:\n"
          f"  build: {shlex.quote(sys.executable)} src/check.py\n"
          "  lint: true\n  test: true\n  check: true\n")
    write(root / "src/check.py", "print('fixture build passes')\n")
    (root / "runtime/logs").mkdir(parents=True, exist_ok=True)


def quiet_call(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            value = fn(*args, **kwargs)
            return {"exit": 0, "value": value}
        except SystemExit as exc:
            return {"exit": exc.code, "error": err.getvalue()[-500:]}


def evidence_probe(root):
    project(root)
    path = task(root, status="in-progress")
    cli = bridge.load_task_cli(str(ROOT), str(root))
    first = quiet_call(cli.cmd_verify, ["TASK-001"])
    write(root / "src/check.py", "raise SystemExit(7)\n")
    second = quiet_call(cli.cmd_verify, ["TASK-001"])
    close = quiet_call(cli.cmd_done, ["TASK-001"])
    return {"initial_verify_exit": first["exit"],
            "after_code_change_verify_exit": second["exit"],
            "close_exit": close["exit"],
            "final_status": tasklib.load_task(str(path))[1]["metadata.status"]}


def selector_probe(root):
    project(root)
    task(root, deps="[TASK-002]", priority="P2", assignee="human-alice")
    task(root, 2, status="blocked")
    task(root, 3, priority="P1")
    selected = tasklib.pick_task(str(root / "runtime/tasks"))
    ran = []
    result = quiet_call(coder.run_once, str(root), coder.parse_args([]), None,
                        str(root / "runtime/logs"),
                        task_cli_fn=lambda *a: 1,
                        llm_fn=lambda *a, **kw: ran.append(kw["task"]) or 0)
    fm = tasklib.load_task(str(root / "runtime/tasks/TASK-001-audit.md"))[1]
    return {"selected": selected, "dependency_status": "blocked",
            "higher_priority_ready": "TASK-003", "start_stub_exit": 1,
            "llm_called_for": ran, "assignee_after": fm["metadata.assignee"],
            "task_status_after": fm["metadata.status"],
            "round_exit": result["value"]}


def scheduling_probe(root):
    entries = [registry.RegistryEntry(p, p, str(root / p), "local")
               for p in ("a", "b")]
    snaps = {}
    for entry in entries:
        path = task(Path(entry.path))
        snaps[entry.id] = {"tasks": [{"name": path.name,
                                      "content": path.read_text()}]}
    rounds = [[c.entry.id for c in policy.select_candidates(
        entries, max_workers=1, snapshots=snaps)] for _ in range(3)]
    # One deterministic interleaving of two independent dispatch processes.
    s1 = state.SchedulerState.load(str(root / "scheduler"))
    s2 = state.SchedulerState.load(str(root / "scheduler"))
    a1 = s1.allocate("a", "TASK-001", "worker-1")
    a2 = s2.allocate("a", "TASK-001", "worker-2")
    s1.save()
    s2.save()
    persisted = state.SchedulerState.load(str(root / "scheduler"))
    candidate = policy.Candidate(entries[0], "TASK-001", "open", "P2")
    calls = []

    class FailingStart:
        def execute(self, entry, name, args, timeout):
            calls.append(name)
            return SimpleNamespace(exit_code=1 if name == "task_start" else 0)

    dispatcher._run_candidate(entries[0], candidate, adapter=FailingStart())
    return {"projects_chosen_each_round": rounds,
            "both_allocations_accepted": bool(a1 and a2),
            "persisted_workers": [a.worker for a in persisted.allocations.values()],
            "commands_after_failed_start": calls,
            "coder_command_arguments": agent_adapter.candidate_commands(candidate)[-1][1]}


def identity_probe(root):
    path = task(root, status="in-review", risk="P1", reviewer_id="designated-bob")
    chosen = reviewer.pick_in_review(str(root / "runtime/tasks"), "different-eve")
    fm = {"metadata.risk": "P2", "metadata.priority": "P0",
          "metadata.approval-ref": "none"}
    return {"designated_reviewer": "designated-bob", "other_reviewer_selected": chosen,
            "local_blocks_priority_P0_risk_P2": tasklib.p0_missing_approval(fm),
            "dispatcher_decision": policy.governance.governance_check("P0", "P2", "none", 0)[0]}


def process_probe(root):
    if os.name != "posix":
        return {"skipped": "POSIX-only process probe"}
    # Only audit-owned processes. A grandchild writes a harmless marker after timeout.
    marker = root / "grandchild-marker"
    code = ("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
            + repr("import time,pathlib; time.sleep(0.4); pathlib.Path("
                   + repr(str(marker)) + ").write_text('survived')")
            + "]); time.sleep(20)")
    started = time.monotonic()
    rc = coder._run_argv([sys.executable, "-c", code], None, timeout=0.15,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.65)
    (root / "runtime/locks").mkdir(parents=True)
    p1 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    p2 = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
    killed = []
    try:
        with mock.patch.object(launcher, "_kill_pid",
                               side_effect=lambda pid: killed.append(pid) or True):
            quiet_call(launcher.record_llm_pid, str(root), p1.pid, "pi", "TASK-001")
            quiet_call(launcher.record_llm_pid, str(root), p2.pid, "pi", "TASK-002")
        return {"timeout_exit": rc, "grandchild_survived": marker.exists(),
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "second_role_attempted_to_kill_first": p1.pid in killed}
    finally:
        for proc in (p1, p2):
            proc.kill()
            proc.wait()


def fatal_probe(root):
    output = root / "normal-output.log"
    with output.open("w") as stream:
        rc = coder._run_argv(
            [sys.executable, "-c",
             "import time; print('Added test for HTTP 401 handling',flush=True); time.sleep(5)"],
            None, timeout=10, stdout=stream, stderr=subprocess.STDOUT,
            fatal_scan_path=str(output), fatal_scan_start=0)
    return {"normal_test_discussion_exit": rc, "expected_without_false_positive": 0}


def worker_probe(root):
    command = {"command_id": "fixture-1", "project_id": "p",
               "command": {"name": "autoloop_coder", "args": []}}
    cfg = {"server_url": "unused", "token": "fixture", "projects": [{"id": "p", "path": str(root)}]}
    attempts = []
    def fail_report(*args):
        attempts.append(1)
        raise OSError("synthetic lost ACK")
    worker = agent_downlink.DownlinkWorker(cfg, SimpleNamespace(info=lambda *a: None, error=lambda *a: None),
        pickup_fn=lambda *a: None, execute_fn=lambda *a: {"status": "done"}, report_fn=fail_report)
    worker._run(command, cfg["projects"][0])
    ticks = [worker.tick() for _ in range(3)]
    return {"result_report_attempts": len(attempts), "later_ticks": ticks,
            "result_spool_files": len(list(root.rglob("*")))}


def monitor_probe(root):
    ms = load("audit_monitor", MONITOR / "server/monitor_server.py")
    tree = ast.parse((MONITOR / "server/monitor_server.py").read_text())
    duplicates = []
    for node in [tree] + [n for n in tree.body if isinstance(n, ast.ClassDef)]:
        names = {}
        for child in node.body:
            if isinstance(child, (ast.ClassDef, ast.FunctionDef)):
                names.setdefault(child.name, []).append(child.lineno)
        duplicates.extend({"scope": getattr(node, "name", "module"), "name": name, "lines": lines}
                          for name, lines in names.items() if len(lines) > 1)
    cfg = {"projects": [{"id": "p", "name": "fixture", "path": str(root / "p"), "transport": "agent"}],
           "poll_interval_seconds": 30}
    write(root / "projects.json", json.dumps(cfg))
    write(root / "agents.json", json.dumps({"fixture-agent": {"token": "fixture-token", "projects": ["p"]}}))
    os.chmod(root / "agents.json", 0o600)
    with mock.patch.object(ms, "load_notify_config", return_value={}):
        instance = ms.State(cfg, quiet=True, start_poller=False,
            db_path=str(root / "history.db"), ingest_db_path=str(root / "ingest.db"),
            downlink_db_path=str(root / "downlink.db"), registration_db_path=str(root / "registration.db"),
            agents_path=str(root / "agents.json"), projects_path=str(root / "projects.json"))
    ms.ApiHandler.state = instance
    ms.ApiHandler.static_dir = str(root)
    instance.ingest.store_task_events("p", [{"seq": 1, "ev": "agent-first"}])
    instance.ingest.append_server_event("p", {"ev": "server-second"})
    instance.ingest.store_task_events("p", [{"seq": 2, "ev": "agent-second"}])
    event_rows = instance.ingest.read_task_events("p", 10)[2]
    def sessions(offset):
        return {"items": [{"task_id": "TASK-001", "files": [{"name": "fixture.jsonl", "lines": ["fixture-private-code"]}]}],
                "cursor": {"TASK-001/fixture.jsonl": offset}, "truncated": False}
    instance.ingest.store_session_deltas("p", sessions(21))
    instance.ingest.store_session_deltas("p", sessions(42))
    session_lines = instance.ingest.read_session_lines("p", "TASK-001", "fixture.jsonl", 10)
    errors = []
    class Handler(ms.ApiHandler):
        def log_message(self, *args):
            pass
    class HttpServer(ms.ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            exc = sys.exc_info()[1]
            errors.append(type(exc).__name__ + ": " + str(exc))
    httpd = HttpServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, authenticated=True):
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer fixture-token"
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = response.read()
            return {"http": response.status, "body": json.loads(data)}
        except Exception as exc:
            return {"client_error": type(exc).__name__}
        finally:
            conn.close()
    try:
        queued, _ = instance.downlink.enqueue("p", "fixture-direct", "autoloop_coder", [], 1800, "fixture")
        endpoints = {
            "enqueue": request("POST", "/api/downlink/commands", {"project_id": "p", "dedup_key": "fixture-http",
                "command": {"name": "autoloop_coder", "args": []}, "timeout_secs": 1800}),
            "pickup": request("GET", "/api/downlink/pickup"),
            "result": request("POST", f"/api/downlink/commands/{queued['command_id']}/result", {"status": "done", "exit_code": 0}),
            "unauthenticated_status": request("GET", "/api/status", authenticated=False),
            "unauthenticated_session": request("GET", "/api/projects/p/sessions?task=TASK-001&file=fixture.jsonl", authenticated=False),
        }
        return {"duplicate_definitions": duplicates, "endpoints": endpoints, "server_errors": errors,
                "agent_event_lost_to_server_seq_collision": not any(r.get("ev") == "agent-second" for r in event_rows),
                "two_identical_new_session_lines_stored": len(session_lines)}
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def telemetry_probe(root):
    for number in range(1, 62):
        task(root, number, status="done" if number <= 50 else "open")
    snapshot = agent_runtime.read_project_runtime(str(root))
    received = agent_payload.build_payload("p", snapshot)["files"]["tasks"]
    events = [{"name": "autoloop-coder-events.jsonl", "content": "old\n" * 1500 + "LATEST-EVENT\n"}]
    clipped = agent_payload.build_payload("p", {"events": events})
    return {"source_task_count": len(snapshot["tasks"]),
            "transmitted_task_count": len(received),
            "transmitted_open_count": sum("status: open" in t["content"] for t in received),
            "latest_role_event_transmitted": "LATEST-EVENT" in clipped["files"]["events"][0]["content"]}


def context_probe(root):
    project(root)
    path = task(root, status="in-progress")
    with path.open("a") as out:
        out.write("\n".join(["fixture line"] * 650))
    plan = context_loader.assemble("TASK-001", root=str(root), budget=600)
    task(root, 1000)
    return {"context_budget": 600, "allocated_lines": plan.allocated,
            "task_1000_visible": "TASK-1000-audit.md" in tasklib.task_files(str(root / "runtime/tasks")),
            "short_id_of_task_1000": tasklib.short_id("TASK-1000-audit")}


def main():
    observations = {}
    probes = [("stale_evidence", evidence_probe), ("task_selection", selector_probe),
              ("scheduler", scheduling_probe), ("identity_and_P0", identity_probe),
              ("process_lifecycle", process_probe), ("fatal_false_positive", fatal_probe),
              ("lost_result", worker_probe), ("telemetry_truncation", telemetry_probe),
              ("context_and_task_ids", context_probe), ("aimonitor", monitor_probe)]
    with tempfile.TemporaryDirectory(prefix="aios-independent-audit-") as tmp:
        for name, fn in probes:
            root = Path(tmp) / name
            root.mkdir()
            try:
                observations[name] = fn(root)
            except Exception as exc:
                observations[name] = {"probe_error": type(exc).__name__ + ": " + str(exc)}
    print(json.dumps(observations, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
