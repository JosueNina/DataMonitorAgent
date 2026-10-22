"""Turn inventory + run history into per-date pipeline states and findings.

The question a check answers is not "did a job fail" (DataFleet already mails that) but "did every
dataset publish every date it should have, and if not, why".
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from .cron import Cron, CronError

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


@dataclass
class Settings:
    missed_grace_minutes: int = 20
    stuck_hours: float = 6
    baseline_min_runs: int = 5


@dataclass
class Finding:
    kind: str
    severity: str
    title: str
    pipeline: str
    dataset_ids: list
    job_ids: list = field(default_factory=list)
    processing_date: date = None
    run_ids: list = field(default_factory=list)
    detail: list = field(default_factory=list)
    diagnosis: object = None
    probes: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    context: dict = field(default_factory=dict)  # offset, dependency_of, ...

    @property
    def key(self):
        when = self.processing_date.isoformat() if self.processing_date else "-"
        return f"{self.kind}|{self.pipeline}|{when}"


@dataclass
class DateState:
    processing_date: date
    state: str  # published, recovered, failed, failed_pending, pending, running, stuck, not_run, empty
    runs: list
    pending_fires: list = field(default_factory=list)  # (job, fire time) still to come for this date
    published_at: datetime = None  # first time the date's data landed in /Data on a node (from the sync logs)
    publish_lag_minutes: float = None  # from the start of the run that published to published_at


@dataclass
class PipelineStatus:
    key: str
    dataset_ids: list
    job_ids: list
    state: str  # ok, degraded, failing, disabled
    dates: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    last_published: date = None  # newest processing date some run published, over the whole run history
    config_since: dict = field(default_factory=dict)  # job id -> first run that matches today's cron and offset


def evaluate_pipeline(pipeline, inventory, runs_by_job, now, start, settings=None):
    settings = settings or Settings()
    jobs = pipeline.jobs(inventory)
    status = PipelineStatus(pipeline.key, pipeline.dataset_ids, pipeline.job_ids, "ok")
    enabled = [j for j in jobs if j.enabled and j.schedule]
    if not enabled:
        status.state = "disabled"
        return status

    def finding(kind, severity, title, **kwargs):
        item = Finding(kind, severity, title, pipeline.key, pipeline.dataset_ids, **kwargs)
        status.findings.append(item)
        return item

    _check_configuration(jobs, enabled, finding, now)

    grace = timedelta(minutes=settings.missed_grace_minutes)
    all_runs = sorted((r for j in jobs for r in runs_by_job.get(j.id, [])), key=lambda r: r.started)
    offsets = {j.id: j.offset for j in jobs}
    backfills = [r for r in all_runs if is_backfill(r, offsets.get(r.job_id, 0))]
    runs = [r for r in all_runs if r not in backfills]

    past_fires, future_fires = [], []
    for job in enabled:
        try:
            cron = Cron(job.schedule, job.timezone)
        except CronError as error:
            finding("bad_schedule", "warning", f"Job {job.id} has a schedule that cannot be read",
                    job_ids=[job.id], detail=[f"{job.schedule!r} ({job.timezone}): {error}"])
            continue
        job_runs = [r for r in runs if r.job_id == job.id]
        since = config_since(job, cron, job_runs, now, grace)
        status.config_since[job.id] = since
        older = [r for r in job_runs if since and r.started < since and r.started >= start]
        if older:
            finding("config_changed", "info",
                    f"Job {job.id} changed its schedule or date offset around {since:%Y-%m-%d %H:%M} UTC",
                    job_ids=[job.id], run_ids=[r.id for r in older][:10],
                    detail=[f"Runs before that do not match '{job.schedule}' {job.timezone} with offset {job.offset}; "
                            "the dates they covered are judged by their runs only, not by today's schedule."])
        for fire in cron.fires_between(start, now + timedelta(hours=48)):
            # Fires before the current configuration took effect never existed: do not expect them.
            if fire <= now and since is not None and fire < since - grace:
                continue
            entry = (job, fire, fire.date() - timedelta(days=job.offset))
            (past_fires if fire <= now else future_fires).append(entry)
    missed = [(job, fire, day) for job, fire, day in past_fires
              if fire <= now - grace and not any(
                  r.job_id == job.id and fire - timedelta(minutes=2) <= r.started <= fire + grace for r in runs)]

    dates = {day for _, _, day in past_fires} | {r.processing_date for r in runs if r.started >= start and r.processing_date}
    for day in sorted(dates):
        day_runs = [r for r in runs if r.processing_date == day]
        pending = [(job, fire) for job, fire, d in past_fires + future_fires if d == day and fire > now - grace
                   and not any(r.job_id == job.id and r.started >= fire - timedelta(minutes=2) for r in day_runs)]
        state = _date_state(day_runs, pending, now, settings, inventory)
        entry = DateState(day, state, day_runs, pending)
        _publication(entry)
        status.dates.append(entry)
        _date_findings(finding, day, state, day_runs, pending, missed, now)
        _sync_findings(finding, day, day_runs)

    _republication_finding(finding, status)
    _backfill_finding(finding, backfills, start)
    status.last_published = max((r.processing_date for r in runs if r.status == "Complete" and r.processing_date
                                 and (r.published or r.sync_skipped)), default=None)
    _freshness_finding(finding, status, enabled, now)
    for item in status.findings:
        if item.kind == "run_stuck" and item.processing_date and not scheduled_date(enabled, item.processing_date):
            # A manual run on a date no cron processes (a Sunday of a Mon-Fri pipeline): stop it, re-run nothing.
            item.context["off_schedule_date"] = True
            item.detail.append(f"No scheduled run processes {item.processing_date}: this run was started by hand.")
    if any(d.state in ("failed", "failed_pending", "not_run") for d in status.dates):
        for item in status.findings:
            if item.kind == "redundancy_disabled":
                item.severity = "warning"

    worst = max((SEVERITY_RANK[f.severity] for f in status.findings), default=0)
    status.state = "failing" if worst >= 3 else "degraded" if worst == 2 else "ok"
    return status


def is_backfill(run, offset, slack_days=3):
    """A run processing a date well before the one its schedule would give it: a manual re-run of history."""
    if run.processing_date is None:
        return False
    return run.processing_date < run.started.date() - timedelta(days=offset + slack_days)


def config_since(job, cron, job_runs, now, grace):
    """Since when the job's current cron and offset have been in force, read from its runs.

    The API only serves today's configuration. A run that started at one of today's fire times and processed
    the date today's offset gives proves the configuration was in force; a run that processed a recent date at
    a time today's cron never fires, with no matching run for that date, is the old configuration at work.
    Returns the start of the oldest run of the newest unbroken stretch of matching runs (None: no evidence).
    """
    if not job_runs:
        return None
    fires = cron.fires_between(min(r.started for r in job_runs) - timedelta(days=1), now + timedelta(minutes=1))
    matched_dates, matched = set(), []
    for run in job_runs:
        fire = next((f for f in fires if f - timedelta(minutes=2) <= run.started <= f + grace), None)
        if fire is not None and run.processing_date == fire.date() - timedelta(days=job.offset):
            matched.append(run)
            matched_dates.add(run.processing_date)
    since = None
    for run in sorted(job_runs, key=lambda r: r.started, reverse=True):
        if run in matched:
            since = run.started
        elif run.processing_date not in matched_dates:
            break  # a recent date processed off today's schedule and offset: the older configuration
    return since


def _backfill_finding(finding, backfills, start):
    recent = [r for r in backfills if r.started >= start]
    if not recent:
        return
    dates = sorted({r.processing_date for r in recent})
    finding("backfill", "info", f"Backfill of {len(dates)} historical date(s), {dates[0]} to {dates[-1]}",
            job_ids=sorted({r.job_id for r in recent}), run_ids=[r.id for r in recent][:20],
            detail=[f"{len(recent)} run rows between {min(r.started for r in recent):%Y-%m-%d %H:%M} and "
                    f"{max(r.started for r in recent):%Y-%m-%d %H:%M} UTC; "
                    f"{sum(1 for r in recent if r.status == 'Failed')} failed. Excluded from the date checks."])


def expected_interval_days(jobs, now):
    """Typical days between two processing dates under the current crons (1 for daily jobs, 7 for weekly)."""
    days = set()
    for job in jobs:
        try:
            cron = Cron(job.schedule, job.timezone)
        except CronError:
            continue
        days |= {f.date() - timedelta(days=job.offset) for f in cron.fires_between(now, now + timedelta(days=28))}
    ordered = sorted(days)
    gaps = sorted((b - a).days for a, b in zip(ordered, ordered[1:]))
    return gaps[len(gaps) // 2] if gaps else None


def _freshness_finding(finding, status, enabled, now):
    """A pipeline whose newest published date is far behind its schedule, whatever the window shows."""
    interval = expected_interval_days(enabled, now)
    if interval is None:
        return
    offset = max(j.offset for j in enabled)
    if status.last_published is None:
        finding("stale", "critical", "Nothing was published in the 14 days of run history",
                job_ids=[j.id for j in enabled],
                detail=["The jobs are enabled but no run published a date. Check that they run at all."])
        return
    behind = (now.date() - timedelta(days=offset) - status.last_published).days
    # Weekends and holidays stretch daily schedules by up to three days; allow two intervals on top of that.
    if behind > 2 * interval + 3:
        finding("stale", "critical", f"No new date published since {status.last_published} ({behind} days)",
                job_ids=[j.id for j in enabled],
                detail=[f"The schedule produces a new processing date about every {interval} day(s)."])


def _check_configuration(jobs, enabled, finding, now):
    primaries = [j for j in enabled if j.queue == "primary"]
    redundancies = [j for j in enabled if j.queue == "redundancy"]
    disabled_redundancies = [j for j in jobs if j.queue == "redundancy" and not j.enabled]
    if primaries and not redundancies:
        if disabled_redundancies:
            # A standing configuration, not an incident: info until a date fails that the redundancy would retry.
            finding("redundancy_disabled", "info", "The redundancy job is disabled: a failed run is not retried",
                    job_ids=[j.id for j in disabled_redundancies],
                    detail=[f"Job {j.id} '{j.name}' is Disabled ({j.schedule} {j.timezone})" for j in disabled_redundancies])
        else:
            finding("no_redundancy", "info", "No redundancy job exists for this pipeline",
                    job_ids=[j.id for j in primaries])
    if redundancies and not primaries:
        finding("primary_disabled", "warning", "Only the redundancy job is enabled",
                job_ids=[j.id for j in redundancies])
    gaps = coverage_gaps(primaries, redundancies, now=now)
    if gaps:
        total, examples = gaps
        finding("offset_mismatch", "warning",
                f"The redundancy does not back up the primary on {len(examples)} of {total} dates",
                job_ids=[j.id for j in primaries + redundancies],
                detail=examples[:4] +
                       ["Processing date = UTC run date - offset. A redundancy that lands on a different date "
                        "cannot republish the date the primary missed (and can turn the primary into Skipped)."])

    commits = {j.commit for j in enabled if j.commit}
    if len(commits) > 1:
        finding("commit_mismatch", "warning", "Jobs of one pipeline run different commits",
                job_ids=[j.id for j in enabled], detail=[f"Job {j.id}: {j.commit}" for j in enabled])


def scheduled_date(jobs, day):
    """Whether some job's cron, with its offset, processes `day` (processing date = UTC run date - offset)."""
    for job in jobs:
        try:
            cron = Cron(job.schedule, job.timezone)
        except CronError:
            return True  # unreadable schedule: do not claim the date is off schedule
        start = datetime.combine(day + timedelta(days=job.offset), time.min, timezone.utc)
        if cron.fires_between(start, start + timedelta(days=1)):
            return True
    return False


def coverage_gaps(primaries, redundancies, days=14, now=None):
    """For the next `days`, primary runs whose processing date no redundancy run at or after it (within 48 h)
    processes.
    Returns (primary runs checked, explanations) or None when everything is covered."""
    if not primaries or not redundancies:
        return None
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=days)

    def fires(jobs):
        out = []
        for job in jobs:
            try:
                cron = Cron(job.schedule, job.timezone)
            except CronError:
                continue
            out.extend((fire, fire.date() - timedelta(days=job.offset), job) for fire in cron.fires_between(now, end))
        return out

    backups = fires(redundancies)
    first_by_day = {}
    for fire, day, job in sorted(fires(primaries)):
        if fire < end - timedelta(hours=48):
            first_by_day.setdefault(day, (fire, job))
    checked, gaps = len(first_by_day), []
    for day, (fire, job) in sorted(first_by_day.items()):
        # Covered when some redundancy run at or after the primary's first run for the date processes the same
        # date: a run at the same minute is independent and publishes the date on its own (Brain Wikipedia runs
        # both jobs at 12:00). High-frequency jobs (hourly) process a date many times; one backup run is enough.
        if any(d == day and fire <= f <= fire + timedelta(hours=48) for f, d, _ in backups):
            continue
        after = sorted((f, d, j) for f, d, j in backups if fire < f <= fire + timedelta(hours=24))
        if after:
            f, d, j = after[0]
            gaps.append(f"Job {job.id} at {fire:%a %Y-%m-%d %H:%M} UTC processes {day}; the next redundancy run "
                        f"(job {j.id} at {f:%a %H:%M} UTC) processes {d}")
        else:
            gaps.append(f"Job {job.id} at {fire:%a %Y-%m-%d %H:%M} UTC processes {day}; no redundancy run for "
                        f"that date follows")
    return (checked, gaps) if gaps else None


def _date_state(day_runs, pending, now, settings, inventory):
    complete = [r for r in day_runs if r.status == "Complete"]
    failed = [r for r in day_runs if r.status == "Failed"]
    running = [r for r in day_runs if r.status in (None, "In Progress")]
    if any(r.published or r.sync_skipped for r in complete):
        return "recovered" if failed else "published"
    if running:
        for run in running:
            job = inventory.jobs.get(run.job_id)
            limit = timedelta(hours=settings.stuck_hours)
            if job and job.timeout_minutes:
                limit = max(timedelta(minutes=job.timeout_minutes * 1.5), timedelta(minutes=30))
            if now - run.started > limit:
                return "stuck"
        return "running"
    # Each chain step is its own run row: a trivial step ("sleep 1s") completes without a sync next to the step
    # that failed, so a completed row without a sync is not a success when another row of the date failed.
    if failed:
        return "failed_pending" if pending else "failed"
    if complete:
        return "empty"
    return "pending" if pending else "not_run"


def _date_findings(finding, day, state, day_runs, pending, missed, now):
    failed = [r for r in day_runs if r.status == "Failed"]
    missed_today = [(job, fire) for job, fire, d in missed if d == day]
    # A job that processed the date at another time ran off-schedule (cron changed, or a manual run): not missed.
    off_schedule = [(job, fire, next(r for r in day_runs if r.job_id == job.id)) for job, fire in missed_today
                    if any(r.job_id == job.id for r in day_runs)]
    missed_today = [(job, fire) for job, fire in missed_today if not any(r.job_id == job.id for r in day_runs)]
    if off_schedule:
        finding("run_off_schedule", "info", f"{day} was processed outside the current schedule",
                processing_date=day, job_ids=sorted({j.id for j, _, _ in off_schedule}),
                run_ids=[r.id for _, _, r in off_schedule],
                detail=[f"Job {j.id} ran at {r.started:%Y-%m-%d %H:%M} UTC instead of {f:%Y-%m-%d %H:%M} UTC "
                        f"({j.schedule} {j.timezone}): the cron changed since, or it was a manual run"
                        for j, f, r in off_schedule])
    if state == "failed":
        finding("date_failed", "critical", f"{day} was not published: every run failed",
                processing_date=day, job_ids=sorted({r.job_id for r in failed}), run_ids=[r.id for r in failed],
                detail=[_run_line(r) for r in failed] + [_missed_line(j, f) for j, f in missed_today])
    elif state == "failed_pending":
        nxt = min(fire for _, fire in pending)
        finding("date_failed_pending", "warning",
                f"{day} failed so far; the next scheduled run is at {nxt:%Y-%m-%d %H:%M} UTC",
                processing_date=day, job_ids=sorted({r.job_id for r in failed}), run_ids=[r.id for r in failed],
                detail=[_run_line(r) for r in failed])
    elif state == "not_run":
        finding("date_not_run", "critical", f"{day} was not published: no scheduled run started",
                processing_date=day, job_ids=sorted({j.id for j, _ in missed_today}),
                detail=[_missed_line(j, f) for j, f in missed_today] or ["No run was found for this date."])
    elif state == "stuck":
        stuck = [r for r in day_runs if r.status in (None, "In Progress")]
        finding("run_stuck", "critical", f"A run for {day} has been in progress for too long",
                processing_date=day, job_ids=sorted({r.job_id for r in stuck}), run_ids=[r.id for r in stuck],
                detail=[f"Run {r.id} of job {r.job_id} started {r.started:%Y-%m-%d %H:%M} UTC "
                        f"({(now - r.started).total_seconds() / 3600:.1f} h ago)" for r in stuck])
    elif state == "empty":
        complete = [r for r in day_runs if r.status == "Complete"]
        finding("date_empty", "info", f"{day} completed but published no files",
                processing_date=day, job_ids=sorted({r.job_id for r in complete}), run_ids=[r.id for r in complete],
                detail=["DataFleet skips the sync when a run writes no files. Expected when the source had "
                        "nothing new; a problem when the source did publish."])
    elif state == "recovered":
        winner = next(r for r in day_runs if r.status == "Complete" and (r.published or r.sync_skipped))
        finding("date_recovered", "info", f"{day} failed first and was published by run {winner.id}",
                processing_date=day, job_ids=sorted({r.job_id for r in failed}),
                run_ids=[r.id for r in failed] + [winner.id],
                detail=[_run_line(r) for r in failed] + [_run_line(winner)])
    if state in ("published", "recovered", "running") and missed_today:
        finding("run_missed", "warning", f"A scheduled run for {day} never started",
                processing_date=day, job_ids=sorted({j.id for j, _ in missed_today}),
                detail=[_missed_line(j, f) for j, f in missed_today])


def _publication(entry):
    """When the date reached /Data, from the sync logs of the runs that published it."""
    best = None
    for run in entry.runs:
        for sync in run.sync:
            info = sync.info
            if sync.status == "Complete" and info and info.synced_at and (best is None or info.synced_at < best[0]):
                best = (info.synced_at, run)
    if best:
        entry.published_at = best[0]
        entry.publish_lag_minutes = round((best[0] - best[1].started).total_seconds() / 60, 1)


def publications(day_runs):
    """(run, decisions) for every run whose sync actually wrote the date to a node."""
    out = []
    for run in day_runs:
        decisions = {s.info.decision for s in run.sync if s.status == "Complete" and s.info}
        if decisions & {"first", "again", "forced"}:
            out.append((run, decisions))
    return out


def _sync_findings(finding, day, day_runs):
    """What the per-node sync logs reveal: failed or diverging nodes, publishers outside the pipeline."""
    for run in day_runs:
        statuses = {s.name: s.status for s in run.sync}
        failed = [s for s in run.sync if s.status == "Failed"]
        if failed:
            covered = {s.name for r in day_runs if r is not run for s in r.sync if s.status == "Complete"}
            missing = [s for s in failed if s.name not in covered]
            if missing:
                finding("sync_failed", "warning",
                        f"{day}: the sync failed on {', '.join(s.name for s in missing)}",
                        processing_date=day, job_ids=[run.job_id], run_ids=[run.id],
                        detail=[f"Run {run.id} sync: " + ", ".join(f"{k} {v}" for k, v in statuses.items())] +
                               [f"{s.name}: {s.info.error}" for s in missing if s.info and s.info.error] +
                               ["Live trading nodes without the files serve stale data for this dataset."])
        elif "Complete" in statuses.values() and "Skipped" in statuses.values():
            finding("sync_diverged", "warning", f"{day}: nodes disagree on run {run.id}",
                    processing_date=day, job_ids=[run.job_id], run_ids=[run.id],
                    detail=[", ".join(f"{k} {v}" for k, v in statuses.items()),
                            "One node took the files and another skipped them: check that every node has the data."])

    published = any(s.status == "Complete" for r in day_runs for s in r.sync)
    skipped = [(r, s) for r in day_runs for s in r.sync if s.status == "Skipped" and s.info and s.info.prior_sync_at]
    if skipped and not published:
        run, sync = skipped[0]
        finding("skipped_unseen", "info",
                f"{day} was skipped because of a sync this pipeline did not make",
                processing_date=day, job_ids=sorted({r.job_id for r, _ in skipped}), run_ids=[r.id for r, _ in skipped],
                detail=[f"Run {run.id} job {run.job_id} on {sync.name}: last successful sync of the date at "
                        f"{sync.info.prior_sync_at:%Y-%m-%d %H:%M} UTC, by no run of this pipeline in the last 14 days.",
                        "Another job with the same chain name, or a manual run, published the date. The data may come "
                        "from a different processor or commit than this pipeline's."])


def _republication_finding(finding, status):
    """One finding per pipeline for dates synchronized more than once (a per-date finding is pure noise for
    pipelines whose redundancy re-publishes by design)."""
    repeated = []
    for entry in status.dates:
        published = publications(entry.runs)
        if len(published) > 1:
            repeated.append((entry.processing_date, published))
    if not repeated:
        return
    systematic = len(repeated) == len(status.dates)
    title = ("Every date is published twice: the redundancy re-publishes instead of skipping" if systematic
             else f"{len(repeated)} of {len(status.dates)} dates were published more than once")
    detail = [f"{day}: " + ", ".join(f"run {r.id} job {r.job_id} ({'manual, Synchronize Always' if 'forced' in d else 'again' if 'again' in d else 'first'})"
                                     for r, d in published) for day, published in repeated[:6]]
    detail.append("Each sync merges its tar over /Data, so the later run's files win. Harmless when both runs produce "
                  "the same data; a silent revision when they do not.")
    if systematic:
        detail.append("Typical cause: primary and redundancy have different chain names, or the redundancy's cron turn "
                      "lets it sync again (Synchronize Once is per turn of the syncing job's cron).")
    finding("published_again", "info", title, job_ids=sorted({r.job_id for _, p in repeated for r, _ in p}),
            run_ids=[r.id for _, p in repeated for r, _ in p][:20], detail=detail)


def _run_line(run):
    sync = ", ".join(f"{s.name} {s.status}" for s in run.sync) or "no sync"
    return (f"Run {run.id} job {run.job_id} ({run.queue}) started {run.started:%Y-%m-%d %H:%M} UTC: "
            f"{run.status} exit={run.exit_code}; {sync}")


def _missed_line(job, fire):
    return f"Job {job.id} '{job.name}' was due at {fire:%Y-%m-%d %H:%M} UTC ({job.schedule} {job.timezone}) and did not start"
