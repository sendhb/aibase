#!/usr/bin/env python3
"""kit/tools/agent/ — AIOS 遥测推送 agent（入口）。

用法:
    python3 agent.py --check-config [--config agent.json]    # 只校验配置（exit 0/1）
    python3 agent.py [--config agent.json] [--once] \
                     [--interval N] [--quiet]                # 启动推送（常驻或单轮）

常驻模式按 agent.json 的 poll_interval_seconds（默认 30s）间隔轮询；
--once 推送一轮后退出（cron / systemd timer）；--interval N 覆盖轮询间隔；
--quiet 只输出错误不输出常规日志；SIGINT/SIGTERM 干净退出（exit 0）。

分层：配置加载/校验由 agent_config 负责（TASK-022），主循环由 agent_loop
负责（TASK-027），两者均 stdlib-only。
"""
import argparse
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_config as cfg_lib  # noqa: E402
import agent_loop as loop_lib  # noqa: E402


def _positive_interval(value):
    """argparse type：正有限秒数；非法抛 ArgumentTypeError（usage + exit 2）。"""
    try:
        fv = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"--interval 必须是数字（秒）: {value!r}")
    if not math.isfinite(fv) or fv <= 0:
        raise argparse.ArgumentTypeError(f"--interval 必须大于 0（秒）: {value!r}")
    return fv


def main(argv=None):
    parser = argparse.ArgumentParser(description="AIOS 遥测推送 agent")
    parser.add_argument("--config", default=None,
                        help="agent.json 路径（默认当前目录 ./agent.json）")
    parser.add_argument("--check-config", action="store_true",
                        help="只加载并校验配置，不启动轮询")
    parser.add_argument("--once", action="store_true",
                        help="单轮：推送一轮后退出（cron / systemd timer 场景）")
    parser.add_argument("--interval", type=_positive_interval, default=None,
                        help="轮询间隔（秒），覆盖 agent.json 的 poll_interval_seconds")
    parser.add_argument("--quiet", action="store_true",
                        help="安静模式：只输出错误，不输出常规日志")
    args = parser.parse_args(argv)

    path = args.config or os.path.join(os.getcwd(), "agent.json")
    try:
        cfg = cfg_lib.load_config(path)
    except cfg_lib.AgentConfigError as e:
        print(f"agent: 配置错误: {e}", file=sys.stderr)
        return 1

    if args.check_config:
        print(f"agent: 配置 OK — {cfg['server_url']}，"
              f"{len(cfg['projects'])} 个项目，"
              f"poll_interval_seconds={cfg['poll_interval_seconds']}")
        return 0

    interval = args.interval if args.interval is not None else cfg["poll_interval_seconds"]
    log = loop_lib.AgentLog(quiet=args.quiet)
    flag = loop_lib.StopFlag()
    loop_lib.install_signal_handlers(flag)

    if args.once:
        pushed, skipped, failed = loop_lib.poll_once(
            cfg, states={}, log=log, clock=time.time
        )
        log.info(f"单轮结束：成功 {pushed}，跳过 {skipped}，失败 {failed}")
        if flag.stopped:
            log.info("收到信号，干净退出")
        return 0

    loop_lib.run_forever(cfg, interval, states={}, log=log, stop_fn=flag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
