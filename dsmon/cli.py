"""Command line: python -m dsmon <command>."""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from .logs import describe_sync, diagnose, parse_sync_log, signal_lines
from .monitor import Monitor
from .probes import probe, probe_url
from .report import summary_line, to_json, to_markdown
from .runs import fetch_runs


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run.py", description="Monitor every QuantConnect dataset: jobs, published data and sources.")
    parser.add_argument("--config", help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="check datasets and report findings")
    check.add_argument("datasets", nargs="*", help="dataset ids or name fragments (default: every dataset with jobs)")
    check.add_argument("--hours", type=float, default=36, help="window to judge (default 36)")
    check.add_argument("--no-probe", action="store_true", help="skip source probes")
    check.add_argument("--quick", action="store_true", help="skip downloading logs of complete runs")
    check.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    check.add_argument("--no-info", action="store_true", help="hide info findings")
    check.add_argument("--out", help="also write the Markdown report to this file")
    check.add_argument("--notify", action="store_true", help="send new findings through the configured channels")

    inventory = sub.add_parser("inventory", help="list datasets and their jobs")
    inventory.add_argument("query", nargs="?", help="dataset id or name fragment")
    inventory.add_argument("--refresh", action="store_true")

    runs = sub.add_parser("runs", help="recent runs of a job")
    runs.add_argument("job_id", type=int)
    runs.add_argument("--days", type=float, default=3)

    log = sub.add_parser("log", help="diagnose and print a run's log")
    log.add_argument("job_id", type=int)
    log.add_argument("run_id", type=int)
    log.add_argument("--tail", type=int, default=40)

    run = sub.add_parser("run", help="the daily check: jobs, published data and sources, one report and one alert")
    run.add_argument("datasets", nargs="*", help="dataset ids or name fragments (default: every dataset with jobs)")
    run.add_argument("--hours", type=float, default=36)
    run.add_argument("--sensor", choices=("auto", "always", "never"), default="auto",
                     help="run the cloud sensor: auto = when the newest snapshot is older than [run] sensor_every_hours")
    run.add_argument("--notify", action="store_true")
    run.add_argument("--quiet", action="store_true", help="print only the summary line")
    run.add_argument("--interval", type=float, default=0,
                     help="keep running: one run every INTERVAL seconds (a service loop); 0 runs once")

    content = sub.add_parser("content", help="check the published data itself (cloud sensor + source comparison)")
    content.add_argument("--reuse", action="store_true", help="judge the newest snapshot without running the sensor")
    content.add_argument("--no-info", action="store_true")
    content.add_argument("--out", help="also write the Markdown report to this file")
    content.add_argument("--notify", action="store_true")

    probe_cmd = sub.add_parser("probe", help="probe a dataset's sources, or a URL")
    probe_cmd.add_argument("target", help="dataset id, name fragment, or URL")

    args = parser.parse_args(argv)
    monitor = Monitor(args.config)
    return {"check": _check, "inventory": _inventory, "runs": _runs, "log": _log, "probe": _probe,
            "content": _content, "run": _run}[args.command](monitor, args)


def _run(monitor, args):
    if not args.interval:
        return _run_once(monitor, args)
    import time
    while True:
        started = time.time()
        try:
            _run_once(monitor, args)
        except Exception as error:  # one bad cycle (API outage) must not stop the service
            print(f"run failed: {error}", file=sys.stderr)
        monitor._inventory = None  # pick up job changes made between cycles
        time.sleep(max(0.0, args.interval - (time.time() - started)))


def _run_once(monitor, args):
    from .report import run_markdown
    result = monitor.run(monitor.resolve_datasets(args.datasets), hours=args.hours, sensor=args.sensor,
                         log=lambda m: print(m, file=sys.stderr))
    check, content = result["check"], result["content"]
    sections = []
    markdown = run_markdown(check, content, monitor.inventory(), sections_out=sections)
    path = monitor.save_report(markdown, check.checked_at)
    from .cases import write_cases
    new_cases = write_cases(monitor.var / "cases", check, sections, monitor.inventory())
    monitor.new_cases = new_cases
    if args.notify:
        from .notify import notify_run
        print(f"notified: {notify_run(monitor, check, content)}", file=sys.stderr)
    if args.quiet:  # the headline and one line per dataset section
        markdown = markdown.split("\n## ", 1)[0]  # the headline and the dataset table
    print(markdown)
    print(f"report: {path}", file=sys.stderr)
    for case_dir in new_cases:
        print(f"new case: {case_dir}", file=sys.stderr)
    worst = max((f.severity for f in check.findings + (content.findings if content else [])),
                key=lambda s: {"critical": 3, "warning": 2, "info": 1}[s], default="ok")
    return 2 if worst == "critical" else 1 if worst == "warning" else 0


def _content(monitor, args):
    from .report import content_markdown
    result = monitor.content_check(run_sensor=not args.reuse,
                                   log=lambda m: print(m, file=sys.stderr))
    markdown = content_markdown(result, monitor.inventory(), include_info=not args.no_info)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    if args.notify:
        from .notify import notify_content
        print(f"notified: {notify_content(monitor, result)}", file=sys.stderr)
    print(markdown)
    return 2 if result.worst == "critical" else 1 if result.worst == "warning" else 0


def _check(monitor, args):
    ids = monitor.resolve_datasets(args.datasets)
    result = monitor.check(ids, hours=args.hours, probe_sources=not args.no_probe, analyse_complete=not args.quick)
    markdown = to_markdown(result, monitor.inventory(), include_info=not args.no_info)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    if args.notify:
        from .notify import notify_new
        sent = notify_new(monitor, result)
        print(f"notified: {sent}", file=sys.stderr)
    print(json.dumps(to_json(result), indent=1) if args.json else markdown)
    print(summary_line(result), file=sys.stderr)
    return 2 if result.worst == "critical" else 1 if result.worst == "warning" else 0


def _inventory(monitor, args):
    inventory = monitor.inventory(refresh=args.refresh)
    ids = monitor.resolve_datasets([args.query]) if args.query else sorted(inventory.datasets)
    for dataset_id in ids:
        dataset = inventory.datasets[dataset_id]
        print(f"{dataset.id:>4} {dataset.name} ({dataset.vendor})")
        for pipeline in inventory.pipelines([dataset_id]):
            for job in pipeline.jobs(inventory):
                state = "Enabled " if job.enabled else "Disabled"
                print(f"       {job.id:>4} {state} {job.queue:<10} {job.schedule:<16} {job.timezone:<17} "
                      f"offset {job.offset}  {job.name}  {job.commit or ''}")
    print(f"inventory fetched {inventory.fetched_at}", file=sys.stderr)
    return 0


def _runs(monitor, args):
    since = datetime.now(timezone.utc) - timedelta(days=args.days)
    job = monitor.inventory().jobs.get(args.job_id)
    runs, _ = fetch_runs(monitor.api, [args.job_id], since=since, offsets={args.job_id: job.offset} if job else None)
    for run in runs[args.job_id]:
        sync = ", ".join(f"{s.name} {s.status}" for s in run.sync) or "no sync"
        print(f"{run.started:%Y-%m-%d %H:%M}Z run {run.id} date {run.processing_date} {run.status} "
              f"exit={run.exit_code} [{sync}]")
    return 0


def _log(monitor, args):
    job = monitor.inventory().jobs.get(args.job_id)
    runs, _ = fetch_runs(monitor.api, [args.job_id], offsets={args.job_id: job.offset} if job else None)
    run = next((r for r in runs[args.job_id] if r.id == args.run_id), None)
    if run is None:
        print(f"run {args.run_id} is not in the last 14 days of job {args.job_id}", file=sys.stderr)
        return 1
    text = monitor.archive.fetch(run)
    if text is None:
        print("the log is not available (expired or still running)", file=sys.stderr)
        return 1
    diagnosis = diagnose(text, exit_code=run.exit_code, status=run.status)
    print(f"category: {diagnosis.category}  http: {diagnosis.http_status}  duration: {diagnosis.duration_s}s")
    print(f"root: {diagnosis.summary}")
    for line in diagnosis.evidence:
        print(f"  evidence: {line[:300]}")
    for sync in run.sync:
        sync_text = monitor.archive.fetch_sync(run, sync)
        print("sync " + describe_sync(sync, parse_sync_log(sync_text) if sync_text else None))
    print("--- tail (noise removed) ---")
    for stamp, level, message in signal_lines(text)[-args.tail:]:
        print(f"{stamp or '':19} {level:5} {message[:300]}")
    return 0


def _probe(monitor, args):
    if args.target.startswith("http"):
        results = [probe_url(args.target)]
    else:
        ids = monitor.resolve_datasets([args.target])
        specs = [s for s in monitor.probe_specs if s.get("dataset") in ids]
        if not specs:
            print("no probe configured for that dataset", file=sys.stderr)
            return 1
        results = [probe(s, contact_user_agent=monitor.contact_user_agent) for s in specs]
    for result in results:
        print(("OK     " if result.ok else "FAILED ") + result.describe())
    return 0 if all(r.ok for r in results) else 1
