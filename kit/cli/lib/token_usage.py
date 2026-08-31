#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""token_usage.py — 任务级 Token 用量记录（轻量估算）

用途：在 autoloop 每轮 LLM 调用结束后，记录 provider、耗时、输出字节数，
并估算 token 消耗，写入 runtime/logs/token-usage.jsonl。

说明：
- 目前多数 LLM CLI 不暴露标准 usage 字段，因此这里先记录“可观测近似值”：
  duration_s、output_bytes、token_estimate（按 output_bytes / 4 粗估）。
- 后续如果 pi / claude / codewhale 输出 usage JSON，可在此解析并覆盖估算值。

用法：
  python3 kit/cli/lib/token_usage.py append \
    --role coder --task TASK-001 --provider pi --outcome ok \
    --duration 42 --bytes 12345
"""
import argparse
import json
import os
import sys
import time


def find_project_root(start):
    d = os.path.dirname(os.path.abspath(start))
    while True:
        if os.path.isfile(os.path.join(d, "aios.config.yaml")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(start)))


def usage_path(root):
    return os.path.join(root, "runtime", "logs", "token-usage.jsonl")


def append(role, task, provider, outcome, duration_s, output_bytes):
    root = find_project_root(__file__)
    path = usage_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        output_bytes = int(output_bytes)
    except (TypeError, ValueError):
        output_bytes = 0
    try:
        duration_s = float(duration_s)
    except (TypeError, ValueError):
        duration_s = 0.0
    token_estimate = max(0, output_bytes // 4)
    record = {
        "ts": time.time(),
        "role": role,
        "task": task,
        "provider": provider,
        "outcome": outcome,
        "duration_s": duration_s,
        "output_bytes": output_bytes,
        "token_estimate": token_estimate,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Append token usage record")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("append")
    p.add_argument("--role", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--duration", required=True)
    p.add_argument("--bytes", required=True)
    args = parser.parse_args(argv)
    path = append(args.role, args.task, args.provider, args.outcome,
                  args.duration, args.bytes)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
