"""Case folders: one per dataset that needs action, the unit the agent works on.

`reports/cases/<YYYY-MM-DD>/<dataset-slug>/` holds `case.md` (the dataset's section of the run report, with its
jobs and repositories) and `case.json` (the findings, which of them are new, and the issue and PR the agent opened
for it). Runs of the same day refresh the same folder. `reports/cases/index.json` remembers the first case of each
finding, so a finding that keeps showing up does not start a new case every hour.

A case stays `open` until someone (or the agent) closes it; retention never deletes an open case.
"""
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

_DAY = 86400


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "dataset"


def write_cases(folder, check, sections, inventory):
    """Write a case per critical or warning section. Returns the case folders that carry new findings."""
    folder = Path(folder)
    index_path = folder / "index.json"
    index = _load(index_path, {})
    day = f"{check.checked_at:%Y-%m-%d}"
    with_new = []
    for label, ids, level, items, markdown in sections:
        if level not in ("critical", "warning"):
            continue
        acting = [f for f in items if f.severity != "info"]
        case_dir = folder / day / slug(label)
        case_dir.mkdir(parents=True, exist_ok=True)
        relative = case_dir.relative_to(folder).as_posix()
        new = [f.key for f in acting if f.key not in index]
        previous = _load(case_dir / "case.json", {})
        jobs = sorted({j for f in acting for j in f.job_ids} |
                      {j for d in ids for j in (inventory.datasets[d].job_ids if d in inventory.datasets else [])})
        repos = sorted({inventory.jobs[j].repo for j in jobs if j in inventory.jobs and inventory.jobs[j].repo})
        case = {
            "label": label,
            "dataset_ids": list(ids),
            "level": level,
            "status": previous.get("status", "open"),
            "created": previous.get("created", check.checked_at.isoformat(timespec="seconds")),
            "updated": check.checked_at.isoformat(timespec="seconds"),
            "repos": repos,
            "jobs": [_job(inventory.jobs[j]) for j in jobs if j in inventory.jobs],
            "findings": [_finding(f) for f in acting],
            "new": new,
            "issue": previous.get("issue"),
            "pr": previous.get("pr"),
        }
        (case_dir / "case.json").write_text(json.dumps(case, indent=1), encoding="utf-8")
        (case_dir / "case.md").write_text(_header(case, check) + "\n" + markdown, encoding="utf-8")
        for item in acting:
            index.setdefault(item.key, {"case": relative, "first_seen": case["updated"]})
        if new:
            with_new.append(case_dir)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=1), encoding="utf-8")
    return with_new


def prune_cases(folder, days=90, now=None):
    """Delete closed cases older than `days`; open ones stay while they are open. Returns cases removed."""
    folder = Path(folder)
    now = now or time.time()
    removed = 0
    for case_json in folder.glob("*/*/case.json"):
        case = _load(case_json, {})
        if case.get("status", "open") == "open":
            continue
        if (now - case_json.stat().st_mtime) / _DAY > days:
            shutil.rmtree(case_json.parent, ignore_errors=True)
            removed += 1
    for day_dir in folder.glob("*"):
        if day_dir.is_dir() and not any(day_dir.iterdir()):
            day_dir.rmdir()
    index_path = folder / "index.json"
    if removed and index_path.exists():
        index = _load(index_path, {})
        kept = {k: v for k, v in index.items() if (folder / v["case"]).exists()}
        index_path.write_text(json.dumps(kept, indent=1), encoding="utf-8")
    return removed


def _header(case, check):
    lines = [f"# Case: {case['label']} ({case['level'].upper()})", "",
             f"Checked at {check.checked_at:%Y-%m-%d %H:%M} UTC. Datasets: "
             + ", ".join(map(str, case["dataset_ids"])) + ".",
             f"New findings in this run: {len(case['new'])} of {len(case['findings'])}.", ""]
    if case["jobs"]:
        lines += ["| Job | Name | Enabled | Queue | Schedule | Offset | Repository |", "|---|---|---|---|---|---|---|"]
        for job in case["jobs"]:
            lines.append(f"| {job['id']} | {job['name']} | {job['enabled']} | {job['queue']} | {job['schedule']} "
                         f"{job['timezone']} | {job['offset']} | {job['commit_url'] or '-'} |")
        lines.append("")
    return "\n".join(lines)


def _job(job):
    return {"id": job.id, "name": job.name, "enabled": job.enabled, "queue": job.queue, "schedule": job.schedule,
            "timezone": job.timezone, "offset": job.offset, "repo": job.repo, "commit_url": job.commit_url}


def _finding(item):
    diagnosis = item.diagnosis
    return {"key": item.key, "kind": item.kind, "severity": item.severity, "title": item.title,
            "processing_date": item.processing_date.isoformat() if item.processing_date else None,
            "job_ids": list(item.job_ids), "run_ids": list(item.run_ids), "detail": list(item.detail),
            "cause": diagnosis.category if diagnosis else item.context.get("cause"),
            "evidence": list(diagnosis.evidence[:5]) if diagnosis else [],
            "actions": list(item.actions or [])}


def _load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
