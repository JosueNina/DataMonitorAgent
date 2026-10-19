"""Retention for the work folder, so a scheduled monitor does not grow without bound.

Limits come from [retention] in config.toml. Failed runs keep their logs longer than complete ones: they are the
evidence people come back for. Sensor snapshots are kept by count, because the content judge only compares with the
last few.
"""
import json
import time
from pathlib import Path

DEFAULTS = {"logs_days": 30, "failed_logs_days": 180, "snapshots": 30, "reports_days": 90, "missing_days": 15}
_DAY = 86400


def prune(var, settings=None, now=None):
    """Delete what is past its limit under the work folder `var`. Returns {kind: files removed}."""
    limits = dict(DEFAULTS, **(settings or {}))
    now = now or time.time()
    var = Path(var)
    removed = {"logs": 0, "snapshots": 0, "reports": 0, "markers": 0}

    for run_dir in (var / "logs").glob("*"):
        if not run_dir.is_dir():
            continue
        runs = {}
        for path in run_dir.iterdir():
            runs.setdefault(path.name.split(".", 1)[0], []).append(path)
        for files in runs.values():
            newest = max(f.stat().st_mtime for f in files)
            age = (now - newest) / _DAY
            if all(f.name.endswith(".missing") for f in files):
                if age > limits["missing_days"]:
                    removed["markers"] += _delete(files)
                continue
            limit = limits["failed_logs_days"] if _failed(files) else limits["logs_days"]
            if age > limit:
                removed["logs"] += _delete(files)

    snapshots = sorted((var / "content").glob("*.json"))
    if len(snapshots) > limits["snapshots"]:
        removed["snapshots"] += _delete(snapshots[: len(snapshots) - limits["snapshots"]])

    reports = [p for p in (var / "runs").glob("*.md") if p.name != "latest.md"]
    removed["reports"] += _delete([p for p in reports if (now - p.stat().st_mtime) / _DAY > limits["reports_days"]])
    return removed


def _failed(files):
    """A run whose cached diagnosis is anything but ok counts as failed."""
    for path in files:
        if path.name.endswith(".diag.json"):
            try:
                return json.loads(path.read_text(encoding="utf-8")).get("category") != "ok"
            except (OSError, ValueError):
                return False
    return False


def _delete(paths):
    count = 0
    for path in paths:
        try:
            path.unlink()
            count += 1
        except OSError:
            pass
    return count
