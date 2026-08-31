#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_downlink_e2e.py — TASK-036 Windows 实机端到端验证（真 server + 真 agent + 真任务）。

链路：真 aimonitor monitor_server（临时数据目录）→ POST 入队 task_start →
真 agent.py --once（downlink worker）拾取 → 第二道闸 → 项目目录内真执行
`python kit/cli/task start TASK-xxx` → 回报 → server 终态 done + 项目任务真实 in-progress。
一次性脚本：全部产物落系统临时目录，验证完即焚。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

AIBASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AIMONITOR = os.path.abspath(os.path.join(AIBASE, "..", "aimonitor"))
sys.path.insert(0, os.path.join(AIMONITOR, "server"))
import monitor_server as ms  # noqa: E402

PY = sys.executable
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
TMP = tempfile.mkdtemp(prefix="downlink-e2e-")
TOK_AGENT = "tok-smoke-agent-001"
TOK_DISP = "tok-smoke-disp-001"
PROJ_ID = "smoke-proj"


def http(method, port, path, body=None, token=None):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=json.dumps(body).encode() if body else None,
                                 method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, json.loads(r.read().decode())


def main():
    failures = []
    # ── 1. 真 aimonitor server（临时目录）───────────────────────────────
    with open(os.path.join(TMP, "projects.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_ID, "name": PROJ_ID,
                                 "path": "D:/nonexistent-smoke"}]}, f)
    agents_path = os.path.join(TMP, "agents.json")
    with open(agents_path, "w", encoding="utf-8") as f:
        json.dump({PROJ_ID + "-agent": {"token": TOK_AGENT, "projects": [PROJ_ID]},
                   "dispatcher": {"token": TOK_DISP, "projects": []}}, f)
    with open(os.path.join(AIMONITOR, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    ms.ApiHandler.state = ms.State(
        config, quiet=True,
        db_path=os.path.join(TMP, "history.db"),
        ingest_db_path=os.path.join(TMP, "ingest.db"),
        registration_db_path=os.path.join(TMP, "registration.db"),
        projects_path=os.path.join(TMP, "projects.json"),
        agents_path=agents_path,
        downlink_db_path=os.path.join(TMP, "downlink.db"))
    httpd = ms.ThreadingHTTPServer(("127.0.0.1", 0), ms.ApiHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("[1] aimonitor server 起于 127.0.0.1:%d" % port)

    # ── 2. mkproject 真项目 + 真任务 ────────────────────────────────────
    proj_dir = os.path.join(TMP, PROJ_ID).replace("\\", "/")
    r = subprocess.run([PY, os.path.join(AIBASE, "kit", "cli", "mkproject"), proj_dir,
                        "--profile", "backend"], cwd=AIBASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        failures.append("mkproject 失败: %s" % r.stderr[-500:])
        return _fin(failures, httpd)
    r = subprocess.run([PY, os.path.join(proj_dir, "kit", "cli", "task"), "new",
                        "downlink smoke 验证任务", "--slug", "downlink-smoke",
                        "--priority", "P3", "--risk", "P3"],
                       cwd=proj_dir, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    out = (r.stdout or "") + (r.stderr or "")
    import re
    m = re.search(r"TASK-[\w-]+\.md", out)
    task_id = m.group(0)[:-3] if m else None
    if not task_id:
        failures.append("task new 未产出任务 id，输出: %s" % out[-400:])
        return _fin(failures, httpd)
    print("[2] 真项目 %s + 真任务 %s" % (proj_dir, task_id))

    # ── 3. dispatcher 入队 task_start ──────────────────────────────────
    st, body = http("POST", port, "/api/downlink/commands",
                    {"project_id": PROJ_ID, "dedup_key": "smoke:%s:start" % task_id,
                     "command": {"name": "task_start", "args": [task_id]},
                     "timeout_secs": 300}, token=TOK_DISP)
    if st != 200:
        failures.append("入队失败 %s: %s" % (st, body))
        return _fin(failures, httpd)
    cid = body["command_id"]
    print("[3] 指令入队 command_id=%s" % cid)

    # ── 4. 真 agent --once（downlink worker 拾取→闸→执行→回报）─────────
    agent_json = os.path.join(TMP, "agent.json")
    with open(agent_json, "w", encoding="utf-8") as f:
        json.dump({"server_url": "http://127.0.0.1:%d" % port, "token": TOK_AGENT,
                   "projects": [{"id": PROJ_ID, "path": proj_dir}],
                   "poll_interval_seconds": 5, "downlink_enabled": True}, f)
    r = subprocess.run([PY, os.path.join(AIBASE, "kit", "tools", "agent", "agent.py"),
                        "--once", "--config", agent_json],
                       cwd=AIBASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    agent_out = (r.stdout or "") + (r.stderr or "")
    print("[4] agent --once exit=%d" % r.returncode)
    for line in agent_out.splitlines():
        if "downlink" in line or "单轮" in line:
            print("    " + line.strip())

    # ── 5. 验证：server 终态 + 项目任务真实状态 ─────────────────────────
    st, body = http("GET", port, "/api/downlink/commands/%s" % cid, token=TOK_DISP)
    cmd = body.get("command") or {}
    status = cmd.get("status")
    exit_code = (cmd.get("result") or {}).get("exit_code")
    print("[5] server 指令终态: status=%s exit_code=%s" % (status, exit_code))
    if status != "done" or exit_code != 0:
        failures.append("server 终态非 done/0: %s %s" % (status, exit_code))
    task_file = os.path.join(proj_dir, "runtime", "tasks", "%s.md" % task_id)
    with open(task_file, encoding="utf-8") as f:
        content = f.read()
    if "status: in-progress" not in content:
        failures.append("项目任务未真实 in-progress（task start 未生效）")
    else:
        print("[6] 项目任务 %s 已真实 in-progress ✓" % task_id)
    return _fin(failures, httpd)


def _fin(failures, httpd):
    httpd.shutdown()
    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except OSError:
        pass
    if failures:
        print("\n✗ 实机验证失败:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\n✓ Windows 实机端到端验证通过（真 server + 真 agent + 真任务 start）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
