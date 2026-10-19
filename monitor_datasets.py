"""The service loop: one job check of every dataset per interval, the cloud sensor once a day, and (with the agent
on) a Claude triage session per case with new findings.

Each cycle is the same as `python run.py run`: one report in reports/runs, case folders in reports/cases, retention
applied. A failed cycle (an API outage) is logged and the loop goes on.
"""
import argparse
import sys
import time
from datetime import datetime, timezone

from dsmon.cli import _run_once
from dsmon.monitor import Monitor


def log(message):
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {message}", file=sys.stderr, flush=True)


def cycle(monitor, args, agent):
    monitor._inventory = None  # pick up jobs and datasets added since the last cycle
    monitor.new_cases = []
    code = _run_once(monitor, args)
    log(f"cycle done, exit {code}, {len(monitor.new_cases)} case(s) with new findings")
    if not agent or not monitor.new_cases:
        return
    from agent_backend import claude_path, triage
    if not claude_path():
        log("agent on but the claude CLI is not on PATH: skipping triage")
        return
    settings = monitor.config.get("agent", {})
    for case_dir in monitor.new_cases[: int(settings.get("max_sessions_per_cycle", 3))]:
        log(f"triage {case_dir}")
        path = triage(case_dir, int(settings.get("timeout_minutes", 20)))
        log(f"  -> {path}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description="DataMonitorAgent service loop.")
    parser.add_argument("datasets", nargs="*", help="dataset ids or name fragments (default: every dataset with jobs)")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--agent", action="store_true", help="triage new cases with the claude CLI")
    parser.add_argument("--interval", type=float, help="minutes between cycles (default: [run] interval_minutes)")
    parser.add_argument("--config", help="path to config.toml")
    args = parser.parse_args(argv)

    monitor = Monitor(args.config)
    agent = args.agent or bool(monitor.config.get("agent", {}).get("enabled", False))
    interval = 60 * (args.interval or float(monitor.config.get("run", {}).get("interval_minutes", 60)))
    run_args = argparse.Namespace(datasets=args.datasets, hours=36, sensor="auto", notify=True, quiet=True)
    log(f"DataMonitorAgent: {'one cycle' if args.once else f'every {interval / 60:g} min'}, "
        f"agent {'on' if agent else 'off'}, reports in {monitor.var}")
    while True:
        started = time.time()
        try:
            cycle(monitor, run_args, agent)
        except KeyboardInterrupt:
            raise
        except Exception as error:  # one bad cycle must not stop the service
            log(f"cycle failed: {type(error).__name__}: {error}")
        if args.once:
            return 0
        time.sleep(max(0.0, interval - (time.time() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
