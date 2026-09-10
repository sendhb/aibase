#!/usr/bin/env python3
"""Run existing AIMonitor suites in a temporary copy, without production data.

The original tests sometimes write ROOT/data or source fixtures, so they must
not be run in the actual AIMonitor checkout during a read-only audit.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
MONITOR = ROOT.parent / "aimonitor"


def main():
    with tempfile.TemporaryDirectory(prefix="aimonitor-audit-suites-") as tmp:
        base = Path(tmp)
        monitor_root = base / "aimonitor"
        for directory in ("server", "test", "src", "scripts"):
            shutil.copytree(MONITOR / directory, monitor_root / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(ROOT / "kit/tools/telemetry", base / "aibase/kit/tools/telemetry",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        fixture_cfg = {"poll_interval_seconds": 30, "heartbeat_stale_threshold_seconds": 300,
            "projects": [{"id": "aimonitor", "name": "audit-monitor", "path": str(monitor_root)},
                         {"id": "aibase", "name": "audit-aibase", "path": str(base / "aibase")}]}
        (monitor_root / "config").mkdir()
        (monitor_root / "config/projects.json").write_text(json.dumps(fixture_cfg))
        (monitor_root / "runtime/tasks").mkdir(parents=True)
        (monitor_root / "runtime/tasks/TASK-001-audit.md").write_text(
            "---\nname: TASK-001-audit\ndescription: fixture\nmetadata:\n"
            "  type: task\n  status: open\n  priority: P2\n---\n# Fixture\n")
        commands = [["bash", "scripts/build.sh"], ["node", "test/hello.test.js"],
                    [sys.executable, "-B", "test/downlink_test.py"],
                    [sys.executable, "-B", "test/server_smoke.py"],
                    [sys.executable, "-B", "test/registration_test.py", "--coverage"],
                    [sys.executable, "-B", "test/security_test.py"]]
        env = {k: v for k, v in os.environ.items() if "NOTIFY" not in k and "WEBHOOK" not in k}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["no_proxy"] = "127.0.0.1,localhost"
        for argv in commands:
            started = time.monotonic()
            try:
                proc = subprocess.run(argv, cwd=monitor_root, env=env, capture_output=True,
                                      text=True, errors="replace", timeout=180)
                result = {"command": " ".join(argv), "exit": proc.returncode,
                          "seconds": round(time.monotonic() - started, 2),
                          "stdout_tail": "\n".join(proc.stdout.splitlines()[-12:]),
                          "stderr_tail": "\n".join(proc.stderr.splitlines()[-14:])}
            except Exception as exc:
                result = {"command": " ".join(argv), "runner_error": type(exc).__name__ + ": " + str(exc)}
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
