#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_v2_integration.py — TASK-038 v2 集成验证（真全链 happy-path + 安全负例）。

拓扑（单机模拟 hub + 远端）：
  真 aimonitor server（临时数据目录）→ 真 dispatcher 子进程（AgentAdapter 传输）
  → 真 agent.py --once（downlink worker，第二道闸）→ 项目目录真执行
  `python kit/cli/task start TASK-xxx` → 回报 → dispatcher 轮询收终态 → state 机 done。

替身声明：smoke 项目的 kit/cli/autoloop 被替换为快速桩（记录+exit 0），
避免真实 LLM 会话——验证对象是 v2 下行链（队列/传输/闸门/状态机），
autoloop 会话本身是 v1 既有机制，不在本验证范围。task CLI 保持真实。

负例（R2-002）：
  1) 错 token 拾取 → 401/403；
  2) 白名单外命令（rm_rf）→ server 入队即 400（不出队）；
  3) 白名单外项目（ghost）→ server 入队即 400（注册表闸）；
  4) 终态指令回报重放 → 409 幂等拒绝（不覆盖）；
  5) agent 停机 → dispatcher 等待超时 failed ×3 → human；server 拾取超时回收 →
     failed(pickup-timeout)。
一次性脚本：全部产物落系统临时目录，验证完即焚。
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

AIBASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AIMONITOR = os.path.abspath(os.path.join(AIBASE, "..", "aimonitor"))
sys.path.insert(0, os.path.join(AIMONITOR, "server"))
import monitor_server as ms  # noqa: E402

PY = sys.executable
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
TMP = tempfile.mkdtemp(prefix="v2-integration-")
PROJ_A = "smoke-proj"     # happy-path 项目（agent 在线）
PROJ_B = "smoke-proj2"    # 掉线项目（agent 停机）
TOK_AGENT = "tok-agent-a"
TOK_AGENT_B = "tok-agent-b"
TOK_DISP = "tok-dispatcher"
DISPATCHER = os.path.join(AIBASE, "kit", "tools", "dispatcher", "dispatcher.py")
AGENT = os.path.join(AIBASE, "kit", "tools", "agent", "agent.py")

STUB_AUTOLOOP = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 集成烟幕替身：替代真 autoloop（避免真实 LLM 会话）；记录 + exit 0
import os
import sys
import time
root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
log = os.path.join(root, "runtime", "logs", "autoloop-stub.log")
os.makedirs(os.path.dirname(log), exist_ok=True)
with open(log, "a", encoding="utf-8") as f:
    f.write("%s args=%s\\n" % (time.strftime("%H:%M:%S"), sys.argv[1:]))
print("[stub] autoloop %s (smoke double, no LLM session)" % " ".join(sys.argv[1:]))
sys.exit(0)
'''


def http(method, path, body=None, token=None):
    req = urllib.request.Request(SERVER + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except OSError:
            raw = ""
        finally:
            e.close()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except ValueError:
            return e.code, raw[:200]


def db_commands(project_id):
    conn = sqlite3.connect(os.path.join(TMP, "downlink.db"))
    try:
        rows = conn.execute(
            "SELECT command_id, status FROM downlink_commands WHERE project_id=? "
            "ORDER BY command_id", (project_id,)).fetchall()
    except sqlite3.OperationalError:
        return []  # 表懒建（首次入队才建）：队列尚空
    finally:
        conn.close()
    return rows


def run_dispatcher(registry, state_dir, timeout=None, extra_env=None, wait=True):
    env = dict(os.environ)
    env["AIOS_DOWNLINK_TOKEN"] = TOK_DISP
    env["AIOS_DOWNLINK_ACK_MARGIN"] = "2"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra_env or {})
    cmd = [PY, DISPATCHER, "run", "--config", registry, "--state-dir", state_dir,
           "--max-workers", "1"]
    if timeout:
        cmd += ["--timeout", str(timeout)]
    if wait:
        return subprocess.run(cmd, cwd=AIBASE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=300)
    return subprocess.Popen(cmd, cwd=AIBASE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", env=env)


def run_agent_once(agent_json):
    return subprocess.run([PY, AGENT, "--once", "--config", agent_json],
                          cwd=AIBASE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)


def mk_project(proj_id, priority):
    proj_dir = os.path.join(TMP, proj_id)
    r = subprocess.run([PY, os.path.join(AIBASE, "kit", "cli", "mkproject"), proj_dir,
                        "--profile", "backend"], cwd=AIBASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    assert r.returncode == 0, "mkproject %s: %s" % (proj_id, r.stderr[-300:])
    r = subprocess.run([PY, os.path.join(proj_dir, "kit", "cli", "task"), "new",
                        "v2 integration 演示任务（不实现任何功能）",
                        "--slug", "v2-demo", "--priority", priority, "--risk", priority],
                       cwd=proj_dir, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, "task new %s: %s" % (proj_id, r.stderr[-300:])
    m = re.search(r"TASK-[\w-]+\.md", (r.stdout or "") + (r.stderr or ""))
    return proj_dir, m.group(0)[:-3]


def _fin(failures, httpd):
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    if failures:
        print("\n✗ 失败:")
        for x in failures:
            print("  -", x)
        return 1
    print("\n✓ TASK-038 v2 集成验证全通：happy-path 全链 + 4 负例 + 掉线 human/回收")
    return 0


def main():
    failures = []
    global SERVER
    # ── 1. 真 aimonitor server（两个项目入 config，poll 2s 加速聚合）────
    with open(os.path.join(TMP, "projects.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_A, "path": os.path.join(TMP, PROJ_A)},
                                {"id": PROJ_B, "path": os.path.join(TMP, PROJ_B)}]}, f)
    with open(os.path.join(TMP, "agents.json"), "w", encoding="utf-8") as f:
        json.dump({PROJ_A + "-agent": {"token": TOK_AGENT, "projects": [PROJ_A]},
                   PROJ_B + "-agent": {"token": TOK_AGENT_B, "projects": [PROJ_B]},
                   "dispatcher": {"token": TOK_DISP, "projects": []}}, f)
    with open(os.path.join(AIMONITOR, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    # State 的 /api/status 聚合读 config["projects"]（projects_path 只管注册表闸）——
    # 替换为两个 smoke 项目并把轮询周期压到 2s 加速烟幕
    config["projects"] = [{"id": PROJ_A, "name": PROJ_A, "path": os.path.join(TMP, PROJ_A)},
                          {"id": PROJ_B, "name": PROJ_B, "path": os.path.join(TMP, PROJ_B)}]
    config["poll_interval_seconds"] = 2
    ms.ApiHandler.state = ms.State(
        config, quiet=True,
        db_path=os.path.join(TMP, "history.db"),
        ingest_db_path=os.path.join(TMP, "ingest.db"),
        registration_db_path=os.path.join(TMP, "registration.db"),
        projects_path=os.path.join(TMP, "projects.json"),
        agents_path=os.path.join(TMP, "agents.json"),
        downlink_db_path=os.path.join(TMP, "downlink.db"))
    httpd = ms.ThreadingHTTPServer(("127.0.0.1", 0), ms.ApiHandler)
    port = httpd.server_address[1]
    SERVER = "http://127.0.0.1:%d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("[1] aimonitor server 起于 127.0.0.1:%d" % port)

    # ── 2. 两个真项目（各 1 个 open 演示任务）；smoke-proj 的 autoloop 换桩 ──
    dir_a, task_a = mk_project(PROJ_A, "P2")
    dir_b, task_b = mk_project(PROJ_B, "P2")
    with open(os.path.join(dir_a, "kit", "cli", "autoloop"), "w",
              encoding="utf-8", newline="\n") as f:
        f.write(STUB_AUTOLOOP)
    print("[2] 项目就绪: %s/%s=%s, %s/%s=%s（autoloop 已换烟幕桩）"
          % (PROJ_A, "task", task_a, PROJ_B, "task", task_b))

    # ── 2b. 等 /api/status 聚合：两项目 open≥1 进 payload（poller 2s 周期，上限 45s）──
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            st_code, body = http("GET", "/api/status")
            projects = (body or {}).get("projects", []) if st_code == 200 else []
            ready = all(
                any(p.get("id") == pid and (p.get("summary") or {}).get("open", 0) >= 1
                    for p in projects)
                for pid in (PROJ_A, PROJ_B))
            if ready:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        failures.append("45s 内两项目 open 任务未进 /api/status 聚合")
        return _fin(failures, httpd)
    print("[2b] 聚合 ✓ 两项目 open 任务已入 /api/status")

    # ── 3. happy-path：dispatcher run（真 AgentAdapter）→ agent ×N → done ──
    reg_a = os.path.join(TMP, "reg-a.json")
    with open(reg_a, "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_A, "path": dir_a.replace("\\", "/"),
                                 "transport": "agent"}],
                   "aimonitor": {"server_url": SERVER}}, f)
    state_a = os.path.join(TMP, "state-a")
    proc = run_dispatcher(reg_a, state_a, timeout=60, wait=False)
    time.sleep(1.5)
    agent_json = os.path.join(TMP, "agent-a.json")
    with open(agent_json, "w", encoding="utf-8") as f:
        json.dump({"server_url": SERVER, "token": TOK_AGENT,
                   "projects": [{"id": PROJ_A, "path": dir_a.replace("\\", "/")}],
                   "poll_interval_seconds": 5, "downlink_enabled": True}, f)
    deadline = time.time() + 90
    agent_rounds = 0
    while time.time() < deadline:
        rows = db_commands(PROJ_A)
        if rows and all(s == "done" for _, s in rows) and len(rows) >= 2:
            break
        run_agent_once(agent_json)
        agent_rounds += 1
    else:
        failures.append("90s 内 %s 指令未全部 done: %s" % (PROJ_A, db_commands(PROJ_A)))
    out, err = proc.communicate(timeout=120)
    print("[3] happy-path: dispatcher rc=%d, agent 轮数=%d, server 指令=%s"
          % (proc.returncode, agent_rounds, db_commands(PROJ_A)))
    if proc.returncode != 0:
        failures.append("dispatcher run rc=%d: %s" % (proc.returncode, (err or out)[-400:]))
    rows = db_commands(PROJ_A)
    if len(rows) < 2 or any(s != "done" for _, s in rows):
        failures.append("happy-path 指令未全部 done: %s" % rows)
    if not os.path.isfile(os.path.join(state_a, "dispatcher-state.json")):
        failures.append("state 文件未生成（dispatcher 未产生分配）")
        return _fin(failures, httpd)
    with open(os.path.join(dir_a, "runtime", "tasks", task_a + ".md"),
              encoding="utf-8") as f:
        if "status: in-progress" not in f.read():
            failures.append("项目任务未真实 in-progress（task start 未生效）")
        else:
            print("    项目任务 %s 真实 in-progress ✓" % task_a)
    stub_log = os.path.join(dir_a, "runtime", "logs", "autoloop-stub.log")
    if not os.path.isfile(stub_log):
        failures.append("autoloop 桩未被执行（无日志）")
    # state 机：allocated→running→done，seq 连续
    with open(os.path.join(state_a, "dispatcher-state.json"), encoding="utf-8") as f:
        st = json.load(f)
    alloc = next((a for a in st["allocations"].values()
                  if a["project_id"] == PROJ_A), None)
    if not alloc or alloc.get("status") != "done":
        failures.append("state 机分配非 done: %s" % alloc)
    with open(os.path.join(state_a, "dispatcher-events.jsonl"), encoding="utf-8") as f:
        events = [json.loads(l) for l in f if l.strip()]
    ev_seq = [e["seq"] for e in events]
    ev_types = [e["ev"] for e in events]
    if ev_seq != list(range(1, len(ev_seq) + 1)):
        failures.append("事件 seq 不连续: %s" % ev_seq)
    for need in ("dispatcher.allocated", "dispatcher.running", "dispatcher.done"):
        if need not in ev_types:
            failures.append("事件轨迹缺 %s: %s" % (need, ev_types))
    if not failures:
        print("    state 机事件轨迹 %s ✓（seq 连续）" % ev_types)
    else:
        print("    事件轨迹: %s / state: %s" % (ev_types, alloc))

    # ── 4. 安全负例（R2-002）───────────────────────────────────────────
    # 4a) 错 token 拾取
    st_code, _ = http("GET", "/api/downlink/pickup", token="tok-wrong-999")
    if st_code not in (401, 403):
        failures.append("4a 错 token 拾取未被拒: HTTP %s" % st_code)
    else:
        print("[4a] 错 token 拾取被拒 ✓ HTTP %s" % st_code)
    # 4b) 白名单外命令 → 入队即 400（不出队）
    st_code, body = http("POST", "/api/downlink/commands",
                         {"project_id": PROJ_A, "dedup_key": "neg:rm",
                          "command": {"name": "rm_rf", "args": []},
                          "timeout_secs": 60}, token=TOK_DISP)
    if st_code != 400:
        failures.append("4b 白名单外命令未被拒: HTTP %s %s" % (st_code, body))
    else:
        print("[4b] 白名单外命令入队即拒 ✓ HTTP 400（%s）"
              % (body if isinstance(body, str) else body.get("error", "")))
    # 4c) 白名单外项目 → 注册表闸 400
    st_code, body = http("POST", "/api/downlink/commands",
                         {"project_id": "ghost-proj", "dedup_key": "neg:ghost",
                          "command": {"name": "task_start", "args": [task_a]},
                          "timeout_secs": 60}, token=TOK_DISP)
    if st_code != 400:
        failures.append("4c 白名单外项目未被拒: HTTP %s %s" % (st_code, body))
    else:
        print("[4c] 白名单外项目入队即拒 ✓ HTTP 400")
    # 4d) 终态指令回报重放 → 409 幂等（回报须用项目属主 token；
    #     dispatcher token 空 projects 会被授权闸 403 拦下——亦是正确语义）
    rows = db_commands(PROJ_A)
    cid0 = rows[0][0]
    st_code, body = http("POST", "/api/downlink/commands/%s/result" % cid0,
                         {"status": "failed", "exit_code": 9,
                          "stdout_tail": "replay", "stderr_tail": "replay",
                          "finished_at": time.time()}, token=TOK_AGENT)
    if st_code != 409:
        failures.append("4d 终态回报重放未被拒: HTTP %s %s" % (st_code, body))
    else:
        print("[4d] 终态指令回报重放被拒 ✓ HTTP 409（幂等不覆盖）")
    st_code, body = http("GET", "/api/downlink/commands/%s" % cid0, token=TOK_DISP)
    if (body.get("command") or {}).get("status") != "done":
        failures.append("4d 重放后指令状态被污染: %s" % body)
    else:
        print("    重放后指令仍为 done ✓")

    # ── 5. 掉线负例：agent 停机 → 等待超时 ×3 → human；server 拾取超时回收 ──
    reg_b = os.path.join(TMP, "reg-b.json")
    with open(reg_b, "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_B, "path": dir_b.replace("\\", "/"),
                                 "transport": "agent"}],
                   "aimonitor": {"server_url": SERVER}}, f)
    state_b = os.path.join(TMP, "state-b")
    for rnd in (1, 2, 3):
        r = run_dispatcher(reg_b, state_b, timeout=3)
        if r.returncode == 0:
            failures.append("5 掉线轮 %d 意外成功（agent 未运行）: %s"
                            % (rnd, (r.stdout or "")[-200:]))
        print("[5] 掉线轮 %d: dispatcher rc=%d（等待超时）" % (rnd, r.returncode))
    with open(os.path.join(state_b, "dispatcher-state.json"), encoding="utf-8") as f:
        st_b = json.load(f)
    alloc_b = next((a for a in st_b["allocations"].values()
                    if a["project_id"] == PROJ_B), None)
    if not alloc_b or alloc_b.get("status") != "human":
        failures.append("5 掉线 3 轮后未转 human: %s" % alloc_b)
    else:
        print("    连败 3 次 → human ✓（%s）" % alloc_b.get("comment", ""))
    with open(os.path.join(state_b, "dispatcher-events.jsonl"), encoding="utf-8") as f:
        ev_b = [json.loads(l) for l in f if l.strip()]
    if not any(e["ev"] == "dispatcher.human" for e in ev_b):
        failures.append("5 事件流缺 dispatcher.human: %s"
                        % [e["ev"] for e in ev_b])
    # server 拾取超时回收（lazy，进程内触发；pickiup_timeout=2s、重投上限 0）
    now = time.time()
    ms.ApiHandler.state.downlink.pickup([PROJ_B], now, pickup_timeout=2, max_requeue=0)
    rows_b = db_commands(PROJ_B)
    conn = sqlite3.connect(os.path.join(TMP, "downlink.db"))
    try:
        detail = conn.execute(
            "SELECT command_id, status, result_json FROM downlink_commands "
            "WHERE project_id=?", (PROJ_B,)).fetchall()
    finally:
        conn.close()
    bad = [d for d in detail
           if d[1] != "failed" or "pickup-timeout" not in (d[2] or "")]
    if bad:
        failures.append("5 server 拾取超时回收未生效: %s" % detail)
    else:
        print("    server 拾取超时回收 → failed(pickup-timeout) ✓（%d 条）" % len(detail))

    # ── 6. 清理 ────────────────────────────────────────────────────────
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    if failures:
        print("\n✗ 失败:")
        for x in failures:
            print("  -", x)
        return 1
    print("\n✓ TASK-038 v2 集成验证全通：happy-path 全链 + 4 负例 + 掉线 human/回收")
    return 0


if __name__ == "__main__":
    sys.exit(main())
