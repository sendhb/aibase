#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smoke_dispatcher_agent_dryrun.py — TASK-037 Windows 实机 dry-run 验证。

验收（TASK-037 ②）：本机 dry-run 对 agent 条目输出判定（不再 skip）。
链路：真 aimonitor monitor_server（临时数据目录，含 1 个 open 任务的项目）→
dispatcher scan / allocate / run --dry-run（注册表顶层 aimonitor.server_url 指向
该 server）→ agent 条目经 /api/status 聚合读计数并产生治理判定候选。
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
TMP = tempfile.mkdtemp(prefix="disp-agent-dryrun-")
PROJ_ID = "smoke-proj"
TOK_AGENT = "tok-smoke-agent-001"


def main():
    failures = []
    # ── 1. 真 aimonitor server（临时目录）───────────────────────────────
    with open(os.path.join(TMP, "projects.json"), "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_ID, "name": PROJ_ID,
                                 "path": "D:/nonexistent-smoke"}]}, f)
    with open(os.path.join(TMP, "agents.json"), "w", encoding="utf-8") as f:
        json.dump({PROJ_ID + "-agent": {"token": TOK_AGENT, "projects": [PROJ_ID]}}, f)
    with open(os.path.join(AIMONITOR, "config", "projects.json"), encoding="utf-8") as f:
        config = json.load(f)
    # State 的 /api/status 聚合读 config["projects"]（projects_path 只管注册表闸）——
    # 须把 smoke 项目注入 config，agent 条目才能被 probe/snapshot 命中；
    # 轮询周期压到 2s 加速烟幕（真配置 30s）
    config.setdefault("projects", []).append(
        {"id": PROJ_ID, "name": PROJ_ID, "path": os.path.join(TMP, PROJ_ID)})
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
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("[1] aimonitor server 起于 127.0.0.1:%d" % port)

    # ── 2. 真项目 + 1 个 open 任务（供 aimonitor 聚合与候选评估）─────────
    proj_dir = os.path.join(TMP, PROJ_ID).replace("\\", "/")
    r = subprocess.run([PY, os.path.join(AIBASE, "kit", "cli", "mkproject"), proj_dir,
                        "--profile", "backend"], cwd=AIBASE, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=120)
    if r.returncode != 0:
        failures.append("mkproject 失败: %s" % r.stderr[-500:])
        return _fin(failures, httpd)
    r = subprocess.run([PY, os.path.join(proj_dir, "kit", "cli", "task"), "new",
                        "dispatcher dryrun 验证任务", "--slug", "disp-dryrun",
                        "--priority", "P2"], cwd=proj_dir,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    if r.returncode != 0:
        failures.append("task new 失败: %s" % r.stderr[-500:])
        return _fin(failures, httpd)
    print("[2] 项目 %s 就绪（1 个 open 任务）" % PROJ_ID)

    # ── 2b. 等 /api/status 聚合：State 后台 poller 周期刷新（已压到 2s），
    #      等 smoke-proj 的 open 任务进入聚合 payload（上限 45s）──
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/status" % port, timeout=10) as r:
                st = json.loads(r.read().decode())
            proj = next((p for p in st.get("projects", [])
                         if p.get("id") == PROJ_ID), None)
            if proj and (proj.get("summary") or {}).get("open", 0) >= 1:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        failures.append("45s 内 smoke-proj open 任务未进 /api/status 聚合")
        return _fin(failures, httpd)
    print("[2b] 聚合 ✓ smoke-proj open=1 已入 /api/status")

    # ── 3. dispatcher 注册表：aimonitor.server_url → 真 server ──────────
    registry_path = os.path.join(TMP, "disp-projects.json")
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump({
            "projects": [
                {"id": PROJ_ID, "name": PROJ_ID, "path": proj_dir.replace("\\", "/"),
                 "transport": "agent"},
                {"id": "local-idle", "name": "local-idle",
                 "path": "D:/definitely-missing-local-proj"},
            ],
            "aimonitor": {"server_url": "http://127.0.0.1:%d" % port},
        }, f)

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    disp = [PY, os.path.join(AIBASE, "kit", "tools", "dispatcher", "dispatcher.py")]

    def run_disp(*cli_args):
        return subprocess.run(disp + list(cli_args) + ["--config", registry_path],
                              cwd=AIBASE, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=60)

    # ── 4. scan：agent 条目经 aimonitor 出计数，不再 skipped ────────────
    r = run_disp("scan")
    if r.returncode != 0:
        failures.append("scan 退出码 %d: %s" % (r.returncode, r.stderr[-300:]))
    elif "skipped(agent-transport)" in r.stdout:
        failures.append("scan 仍输出 skipped(agent-transport)（TASK-037 目标未达成）")
    elif PROJ_ID not in r.stdout or "open=1" not in r.stdout:
        failures.append("scan 未对 agent 条目输出计数: %r" % r.stdout[-300:])
    else:
        print("[3] scan ✓ agent 条目经 aimonitor 聚合出计数（无 skipped）")
    if "local-idle" not in r.stdout:
        failures.append("scan 本地条目缺失: %r" % r.stdout[-300:])

    # ── 5. allocate / run --dry-run：agent 条目输出判定候选 ─────────────
    r2 = run_disp("allocate")
    if r2.returncode != 0:
        failures.append("allocate 退出码 %d: %s" % (r2.returncode, r2.stderr[-300:]))
    elif "TASK-" not in r2.stdout or PROJ_ID not in r2.stdout:
        failures.append("allocate 未对 agent 条目输出候选: %r" % r2.stdout[-300:])
    else:
        print("[4] allocate ✓ agent 条目出候选（%s）" % PROJ_ID)

    r3 = run_disp("run", "--dry-run")
    if r3.returncode != 0:
        failures.append("run --dry-run 退出码 %d: %s" % (r3.returncode, r3.stderr[-300:]))
    elif "[ok]" not in r3.stdout or PROJ_ID not in r3.stdout:
        failures.append("run --dry-run 未对 agent 条目输出 [ok] 判定: %r" % r3.stdout[-300:])
    else:
        print("[5] run --dry-run ✓ agent 条目 [ok] 判定（不再 skip）")

    # ── 6. 兼容旧约：无 aimonitor.server_url 的注册表 → skipped(agent-transport)
    legacy_path = os.path.join(TMP, "disp-legacy.json")
    with open(legacy_path, "w", encoding="utf-8") as f:
        json.dump({"projects": [{"id": PROJ_ID, "name": PROJ_ID,
                                 "path": proj_dir.replace("\\", "/"),
                                 "transport": "agent"}]}, f)
    r4 = subprocess.run(disp + ["scan", "--config", legacy_path], cwd=AIBASE,
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", env=env, timeout=60)
    if r4.returncode != 0 or r4.stdout.count("skipped(agent-transport)") != 1:
        failures.append("legacy 无 aimonitor 段注册表未保持 skip 行为: %r" % r4.stdout[-200:])
    else:
        print("[6] 兼容 ✓ 未配 aimonitor.server_url 时保持 skipped(agent-transport)")

    return _fin(failures, httpd)


def _fin(failures, httpd):
    httpd.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
    if failures:
        print("\n✗ 失败:")
        for f in failures:
            print("  -", f)
        return 1
    print("\n✓ TASK-037 dry-run 验证全通：agent 条目经 aimonitor 聚合出计数与判定，不再 skip")
    return 0


if __name__ == "__main__":
    sys.exit(main())
