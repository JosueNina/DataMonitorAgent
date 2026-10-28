"""Orchestrates a check: inventory -> runs -> pipeline states -> diagnosis, probes, dependencies -> actions."""
import json
import os
import statistics
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import QCApi
from .health import SEVERITY_RANK, Finding, Settings, evaluate_pipeline, is_backfill
from .holidays import non_business_reason
from .inventory import Inventory, fetch_inventory
from .logs import LogArchive, diagnose, parse_sync_log
from .probes import CONTACT_USER_AGENT, probe, probe_url
from .runbook import CATEGORY_TEXT, actions_for
from .runs import fetch_runs

ROOT = Path(__file__).resolve().parent.parent
_FAILURE_KINDS = {"date_failed", "date_failed_pending", "date_recovered"}
CAUSE_LABELS = {
    "source_bad_content": "the source answered with an unusable body",
    "source_tls": "TLS errors with the source",
    "source_access_denied": "the source refuses access",
    "source_not_published": "the source had not published the file",
    "source_unavailable": "the source was down or timed out",
    "missing_input": "an input from another job was missing",
    "infra": "infrastructure: memory, disk or killed",
    "code_error": "a processor exception",
    "unknown": "cause not recognized",
}


@dataclass
class CheckResult:
    checked_at: datetime
    hours: float
    dataset_ids: list
    pipelines: list
    findings: list
    capped_jobs: list = field(default_factory=list)
    dependency_ids: list = field(default_factory=list)
    previous_check_at: datetime = None
    runs_since_previous: list = field(default_factory=list)  # runs that started after the previous check

    @property
    def worst(self):
        return max((f.severity for f in self.findings), key=lambda s: SEVERITY_RANK[s], default="ok")


@dataclass
class ContentResult:
    sensed_at: datetime
    payload: dict
    findings: list
    notes: list
    snapshots_before: int
    cloud: dict
    mapping: dict

    @property
    def worst(self):
        return max((f.severity for f in self.findings), key=lambda s: SEVERITY_RANK[s], default="ok")


def content_actions(item):
    kind = item.kind
    if kind == "content_missing_dates":
        return ["Check whether the job ran for those dates (the `check` command with a long `--hours`). If it did not, "
                "a Manual Run per date fills them; if it ran and wrote nothing, read its log."]
    if kind == "content_thin_date":
        return ["Read the log of the run that published that date; compare the source for that day. A re-run "
                "(Synchronize Always) republishes the full date once the cause is gone."]
    if kind == "content_identity_collision":
        return ["Look up both security identifiers in the map files: one of the rows belongs to another company. "
                "Usually a stale ticker mapping in the processor."]
    if kind == "content_unsorted":
        return ["Fix the processor's merge so it sorts by date before writing, then republish the affected files "
                "complete (an upload never deletes, every overwritten file must be whole)."]
    if kind in ("content_dups_grew", "content_bad_grew"):
        return ["Compare the worst files with their previous version: a merge that appends without de-duplicating "
                "or a source format change. Fix the processor, then republish the affected files."]
    if kind == "content_too_old":
        return ["Check the source's newest period: if the source already published a newer one, the processor is "
                "not writing it; read its latest log for that report (the `log` command) before re-running anything.",
                "If the source has nothing newer, raise the folder's limit in [content.max_age_days]: the source's "
                "publication lag is longer than configured."]
    if kind == "content_stale":
        return ["The data stopped advancing while the snapshots kept coming: check the jobs (the `check` command) and the "
                "source (the `probe` command)."]
    if kind == "reconcile_mismatch":
        return ["Re-download the dates from the source and diff with the published rows; if the source revised them, "
                "decide whether the dataset should follow revisions. Otherwise it is a processor bug."]
    if kind in ("content_unreadable", "reconcile_unavailable"):
        return ["Open the file from a research notebook (Globals.DataFolder) to see what is wrong with it."]
    return []


class Monitor:
    def __init__(self, config_path=None, var_dir=None, api=None):
        self.config_path = Path(config_path or ROOT / "config.toml")
        with open(self.config_path, "rb") as handle:
            self.config = tomllib.load(handle)
        section = self.config.get("monitor", {})
        self.settings = Settings(section.get("missed_grace_minutes", 20), section.get("stuck_hours", 6),
                                 section.get("baseline_min_runs", 5))
        self.contact_user_agent = section.get("contact_user_agent", CONTACT_USER_AGENT)
        self.dependencies = {int(k): list(v) for k, v in self.config.get("dependencies", {}).items()}
        self.self_healing = set(self.config.get("self_healing", {}).get("datasets", []))
        self.probe_specs = self.config.get("probe", [])
        self.var = Path(var_dir or os.environ.get("DSMON_REPORTS") or ROOT / "reports")
        self.var.mkdir(parents=True, exist_ok=True)
        self.archive = LogArchive(self.var / "logs")
        self._api = api
        self._inventory = None

    @property
    def api(self):
        if self._api is None:
            self._api = QCApi()
        return self._api

    # ----------------------------------------------------------------------------------------- inventory
    def inventory(self, refresh=False, max_age_hours=12):
        if self._inventory is not None and not refresh:
            return self._inventory
        path = self.var / "inventory.json"
        if path.exists() and not refresh:
            cached = Inventory.from_json(json.loads(path.read_text(encoding="utf-8")))
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached.fetched_at)
            if age < timedelta(hours=max_age_hours):
                self._inventory = cached
                return cached
        self._inventory = fetch_inventory(self.api)
        path.write_text(json.dumps(self._inventory.to_json(), indent=1), encoding="utf-8")
        return self._inventory

    def all_dataset_ids(self):
        """Every dataset that has at least one job: the default scope."""
        return sorted(d.id for d in self.inventory().datasets.values() if d.job_ids)

    def resolve_datasets(self, queries=None):
        inventory = self.inventory()
        if not queries:
            return self.all_dataset_ids()
        ids = []
        for query in queries:
            found = inventory.find_datasets(query)
            if not found:
                raise ValueError(f"no dataset matches {query!r}")
            ids.extend(found)
        return sorted(set(ids))

    # ------------------------------------------------------------------------------------------- check
    def run(self, dataset_ids=None, hours=36, sensor="auto", log=print):
        """The daily check. The cloud sensor runs when its newest snapshot is older than [run] sensor_every_hours
        (a job check every few hours, one backtest a day); a sensor failure becomes a finding, not a crash."""
        from .content import SnapshotStore
        check = self.check(dataset_ids, hours=hours)
        every = float(self.config.get("run", {}).get("sensor_every_hours", 20))
        # The sensor reads what the daily jobs published, so a cycle before they have run would only read yesterday.
        # A first install has no snapshot at all: it runs the sensor right away.
        after_hour = int(self.config.get("run", {}).get("sensor_after_hour_utc", 0))
        history = SnapshotStore(self.var / "content").history(limit=1)
        age_hours = None
        if history:
            age_hours = (datetime.now(timezone.utc) - datetime.fromtimestamp(history[-1].get("at", 0), timezone.utc)
                         ).total_seconds() / 3600
        run_sensor = sensor == "always" or (sensor == "auto" and (age_hours is None or age_hours >= every)
                                            and (age_hours is None or check.checked_at.hour >= after_hour))
        content = None
        if sensor != "never" or history:
            try:
                content = self.content_check(run_sensor=run_sensor, log=log)
            except Exception as error:
                check.findings.append(Finding("content_sensor_failed", "warning",
                                              "The published-data check could not run", "dataset-watch", [],
                                              detail=[str(error)[:500]],
                                              actions=["Run the `content` command by hand to see the error; a busy "
                                                       "backtest queue or a compile error in the sensor are usual."]))
        from .cases import prune_cases
        from .retention import prune
        retention = self.config.get("retention", {})
        prune(self.var, retention)
        prune_cases(self.var / "cases", retention.get("cases_days", 90))
        return {"check": check, "content": content}

    def save_report(self, markdown, when):
        folder = self.var / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{when:%Y-%m-%d_%H%M}.md"
        path.write_text(markdown, encoding="utf-8")
        (folder / "latest.md").write_text(markdown, encoding="utf-8")
        return path

    def content_check(self, run_sensor=True, only_mapped=False, log=print):
        """Content layer: run the DatasetWatch sensor in the cloud (or reuse the newest snapshot), judge it against
        the earlier snapshots, and reconcile the configured series with their sources."""
        from .cloud import run_sensor as cloud_run
        from .content import SnapshotStore, judge
        from .reconcile import reconcile

        from .mapping import auto_paths
        settings = self.config.get("content", {})
        specs = self.config.get("reconcile", [])
        store = SnapshotStore(self.var / "content")
        ids = None
        if run_sensor:
            parameters = {"roots": settings.get("roots", "alternative"), "peek": settings.get("peek", ""),
                          "tails": ",".join(sorted({s["file"] for s in specs}))}
            payload, ids, _ = cloud_run(self.api, settings.get("project", "DatasetWatchSensor"), parameters, log=log)
            store.save(payload)
        history = store.history()
        if not history:
            raise RuntimeError("no sensor snapshot yet: run without --reuse first")
        current, before = history[-1], history[:-1]
        published = [d["p"] for d in current.get("datasets", []) if "p" in d]
        mapping = auto_paths(published, self.inventory(), settings.get("paths", {}))
        report = settings.get("report", {})
        findings = judge(current, before, mapping, only_mapped=only_mapped,
                         max_age=settings.get("max_age_days", {}), ignore=report.get("ignore", []),
                         calendars=settings.get("calendars", {}))
        reconciled, notes = reconcile(specs, current.get("tails", {}))
        findings.extend(reconciled)
        for item in findings:
            item.actions = content_actions(item)
        findings.sort(key=lambda f: (-SEVERITY_RANK[f.severity], f.pipeline))
        result = ContentResult(datetime.fromtimestamp(current.get("at", 0), timezone.utc), current, findings, notes,
                               len(before), ids, mapping)
        result.settings = settings
        return result

    def check(self, dataset_ids=None, hours=36, probe_sources=True, analyse_complete=True, now=None):
        now = now or datetime.now(timezone.utc)
        inventory = self.inventory()
        dataset_ids = list(dataset_ids if dataset_ids is not None else self.all_dataset_ids())
        dependency_ids = sorted({d for ds in dataset_ids for d in self.dependencies.get(ds, [])} - set(dataset_ids))
        pipelines = inventory.pipelines(dataset_ids + dependency_ids)
        job_ids = sorted({j for p in pipelines for j in p.job_ids})
        offsets = {j: inventory.jobs[j].offset for j in job_ids}
        runs_by_job, capped = fetch_runs(self.api, job_ids, offsets=offsets)

        start = now - timedelta(hours=hours)
        self._load_sync_logs(runs_by_job, start - timedelta(days=2))
        statuses = [evaluate_pipeline(p, inventory, runs_by_job, now, start, self.settings) for p in pipelines]
        status_by_dataset = {}
        for status in statuses:
            for dataset_id in status.dataset_ids:
                status_by_dataset.setdefault(dataset_id, []).append(status)

        watched_jobs = {j for p in pipelines if set(p.dataset_ids) & set(dataset_ids) for j in p.job_ids}
        self._archive_logs(runs_by_job, now, watched_jobs if analyse_complete else set())
        probe_cache = {}
        dependents = {}
        for dataset_id in dataset_ids:
            for dependency in self.dependencies.get(dataset_id, []):
                dependents.setdefault(dependency, []).append(dataset_id)
        for status in statuses:
            primary = inventory.jobs[status.job_ids[0]]
            for item in status.findings:
                item.context["offset"] = primary.offset
                if not set(item.dataset_ids) & set(dataset_ids):
                    item.context["dependency_of"] = sorted({d for ds in item.dataset_ids for d in dependents.get(ds, [])})
                if item.kind in _FAILURE_KINDS or item.kind == "run_stuck":
                    self._diagnose_finding(item, runs_by_job)
                if probe_sources and item.diagnosis and item.diagnosis.category.startswith("source_") \
                        and item.kind != "date_recovered":
                    item.probes = self._probes_for(item, probe_cache)
                if item.diagnosis and item.diagnosis.category == "missing_input":
                    item.detail.extend(self._dependency_lines(item, status_by_dataset))
            if analyse_complete:
                for item in self._complete_run_findings(status, runs_by_job, start):
                    item.context["offset"] = primary.offset
                    status.findings.append(item)
            self._apply_self_healing(status)
            recurring = self._recurring_failure(status, runs_by_job, now)
            if recurring:
                recurring.context["offset"] = primary.offset
                if not set(recurring.dataset_ids) & set(dataset_ids):
                    recurring.context["dependency_of"] = sorted({d for ds in recurring.dataset_ids
                                                                 for d in dependents.get(ds, [])})
                status.findings.insert(0, recurring)
            for item in status.findings:
                item.actions = actions_for(item)

        dependency_set = set(dependency_ids)
        findings = [f for s in statuses for f in s.findings
                    if not set(f.dataset_ids) <= dependency_set or f.severity != "info"]
        # A dataset that keeps failing leads the report: it says more than any single date does.
        findings.sort(key=lambda f: (-SEVERITY_RANK[f.severity], f.kind != "recurring_failure", f.pipeline,
                                     str(f.processing_date)))
        result = CheckResult(now, hours, dataset_ids, statuses, findings, capped, dependency_ids)
        result.previous_check_at = self.previous_check_at()
        if result.previous_check_at:
            result.runs_since_previous = sorted(
                (r for runs in runs_by_job.values() for r in runs if r.started >= result.previous_check_at),
                key=lambda r: r.started)
        return result

    def previous_check_at(self):
        """Time of the last recorded check, if any."""
        path = self.var / "dsmon.sqlite"
        if not path.exists():
            return None
        import sqlite3
        with sqlite3.connect(path) as db:
            row = db.execute("SELECT checked_at FROM checks ORDER BY id DESC LIMIT 1").fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    # ---------------------------------------------------------------------------------------- helpers
    def _archive_logs(self, runs_by_job, now, complete_jobs):
        """Logs expire after about 7 days: archive the failed runs we can still reach, and the complete ones of
        `complete_jobs` (the watched datasets; dependencies only need their failures)."""
        horizon = now - timedelta(days=8)
        todo = [r for runs in runs_by_job.values() for r in runs
                if r.started >= horizon and r.status in ("Failed", "Complete") and r.log_url
                and (r.status == "Failed" or r.job_id in complete_jobs)
                and not self.archive.has(r) and not self.archive.known_missing(r)]
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(self.archive.fetch, todo))

    def _load_sync_logs(self, runs_by_job, since):
        """Read (and archive) every node's sync log for runs from `since` on."""
        todo = [(r, s) for runs in runs_by_job.values() for r in runs if r.started >= since for s in r.sync if s.url]

        def load(item):
            run, sync = item
            text = self.archive.fetch_sync(run, sync)
            if text:
                sync.info = parse_sync_log(text)

        with ThreadPoolExecutor(8) as pool:
            list(pool.map(load, todo))

    def run_diagnosis(self, run):
        cached = self.archive.cached_diagnosis(run)
        if cached is not None:
            return cached
        text = self.archive.fetch(run)
        if text is None:
            return None
        diagnosis = diagnose(text, exit_code=run.exit_code, status=run.status)
        self.archive.store_diagnosis(run, diagnosis)
        return diagnosis

    def _diagnose_finding(self, item, runs_by_job):
        runs = {r.id: r for rs in runs_by_job.values() for r in rs}
        failed = [runs[i] for i in item.run_ids if i in runs and runs[i].status == "Failed"]
        if not failed:
            return
        latest = max(failed, key=lambda r: r.started)
        diagnosis = self.run_diagnosis(latest)
        if diagnosis is None:
            item.detail.append(f"The log of run {latest.id} is no longer available.")
            return
        item.diagnosis = diagnosis
        item.detail.append(f"Cause ({diagnosis.category}): {CATEGORY_TEXT.get(diagnosis.category, '')}")
        item.detail.append(f"Root error in run {latest.id}: {diagnosis.summary[:300]}")
        others = [self.run_diagnosis(r) for r in failed if r is not latest]
        categories = {d.category for d in others if d}
        if categories - {diagnosis.category}:
            item.detail.append(f"Other failed runs of this date: {', '.join(sorted(categories))}")
        if item.processing_date and diagnosis.category in ("source_not_published", "missing_input"):
            reason = non_business_reason(item.processing_date)
            if reason:
                label = "weekend" if reason == "weekend" else f"holiday: {reason}"
                item.detail.append(f"{item.processing_date} is a US {label}.")
                if item.kind == "date_failed":
                    # The source had nothing to publish; the processor failing on it is noise, not an outage.
                    item.severity = "warning"
                    item.title += f" (probably expected: {label})"
                    item.context["non_business_day"] = reason

    def _probes_for(self, item, cache):
        results = []
        for dataset_id in item.dataset_ids:
            if dataset_id not in cache:
                specs = [s for s in self.probe_specs if s.get("dataset") == dataset_id]
                cache[dataset_id] = [probe(s, contact_user_agent=self.contact_user_agent) for s in specs]
            results.extend(cache[dataset_id])
        for url in (item.diagnosis.urls if item.diagnosis else [])[:3]:
            if url not in cache:
                cache[url] = [probe_url(url)]
            results.extend(cache[url])
        seen, unique = set(), []
        for result in results:
            if result.url not in seen:
                seen.add(result.url)
                unique.append(result)
        if not unique:
            item.detail.append("No probe is configured for this dataset's source (add a [[probe]] to config.toml).")
        return unique

    def _dependency_lines(self, item, status_by_dataset):
        lines = []
        for dataset_id in item.dataset_ids:
            for dependency in self.dependencies.get(dataset_id, []):
                name = self.inventory().datasets.get(dependency)
                for status in status_by_dataset.get(dependency, []):
                    day = next((d for d in status.dates if d.processing_date == item.processing_date), None)
                    state = day.state if day else "no run in the window"
                    lines.append(f"Dependency {dependency} '{name.name if name else '?'}' ({status.key}) for "
                                 f"{item.processing_date}: {state}")
        return lines

    def _recurring_failure(self, status, runs_by_job, now, days=7):
        """One finding when a pipeline failed on several dates of the last week, with the causes. Each failed
        date is also its own finding; this one says the dataset is failing, not that a date was missed."""
        inventory = self.inventory()
        since = now - timedelta(days=days)
        attempts = {}
        for job_id in status.job_ids:
            offset = inventory.jobs[job_id].offset if job_id in inventory.jobs else 0
            for run in runs_by_job.get(job_id, []):
                if run.started < since or run.processing_date is None or is_backfill(run, offset):
                    continue
                key = (job_id, run.processing_date)
                entry = attempts.setdefault(key, {"failed": [], "ok": False})
                if run.status == "Failed":
                    entry["failed"].append(run)
                elif run.status == "Complete" and (run.published or run.sync_skipped):
                    entry["ok"] = True
        failed = {k: v for k, v in attempts.items() if v["failed"] and not v["ok"]}
        # Repeatedly means on two or more dates; a primary and its redundancy failing the same date is one.
        if len({day for _, day in failed}) < 2:
            return None
        causes, lines, lost, latest = {}, [], set(), None
        healing = bool(set(status.dataset_ids) & self.self_healing)
        for (job_id, day), entry in sorted(failed.items(), key=lambda kv: kv[0][1]):
            run = max(entry["failed"], key=lambda r: r.started)
            diagnosis = self.run_diagnosis(run)
            category = diagnosis.category if diagnosis else "unknown"
            causes[category] = causes.get(category, 0) + 1
            if diagnosis and (latest is None or run.started > latest[0].started):
                latest = (run, diagnosis)
            recovered = any(v["ok"] for (j, d), v in attempts.items() if d == day and j != job_id)
            # A date is lost when no job published it, unless a self-healing processor published a later one.
            healed = healing and status.last_published is not None and status.last_published > day
            if not recovered and not healed:
                lost.add(day)
            lines.append(f"{day} job {job_id} run {run.id}: {category}"
                         + (" (another job published the date)" if recovered else ""))
        total = len(attempts)
        main, count = max(causes.items(), key=lambda kv: kv[1])
        same = "same cause" if count == len(failed) else f"{count} with the same cause"
        if main == "unknown":
            same = "cause not recognized" if count == len(failed) else f"{count} with a cause not recognized"
        # Critical once two dates of the week were left unpublished; a redundancy that covered every failure, or a
        # single lost date (already its own critical finding), keeps it a warning.
        severity = "critical" if len(lost) >= 2 else "warning"
        # Nothing lost and the newest two dates published: it failed, recovered, and nobody has to act (BLS after a
        # weekend of 503s from its API). Kept as info so the history still shows it.
        # Recovered means the jobs that failed succeed again on their own newest two dates; a primary that keeps
        # failing while its redundancy publishes (Spectral) is still failing.
        recovered_now = True
        for job_id in {j for j, _ in failed}:
            own = sorted((d, v["ok"]) for (j, d), v in attempts.items() if j == job_id)[-2:]
            recovered_now &= len(own) == 2 and all(ok for _, ok in own)
        if not lost and recovered_now:
            severity = "info"
        item = Finding("recurring_failure", severity,
                       f"Failing repeatedly: {len(failed)} of the last {total} runs failed, {same}"
                       + ("" if main == "unknown" else f" ({CAUSE_LABELS.get(main, main)})"),
                       status.key, status.dataset_ids, job_ids=sorted({j for j, _ in failed}),
                       detail=lines + [f"Last {days} days, one attempt per job and processing date."])
        item.context["cause"] = main
        if latest:
            # The newest failed log's own lines, so whoever reads the case can check the cause label against it.
            run, diagnosis = latest
            item.diagnosis = diagnosis
            item.run_ids = [run.id]
            item.detail.append(f"Newest failed run {run.id}: {diagnosis.summary[:300]}")
        item.context["has_redundancy"] = any(inventory.jobs[j].enabled and inventory.jobs[j].queue == "redundancy"
                                             for j in status.job_ids if j in inventory.jobs)
        return item

    def _apply_self_healing(self, status):
        """A failed date of a self-healing dataset is not lost once a later date got published."""
        if not set(status.dataset_ids) & self.self_healing:
            return
        published = sorted(d.processing_date for d in status.dates if d.state in ("published", "recovered"))
        if status.last_published:
            published.append(status.last_published)
        for item in status.findings:
            if item.kind != "date_failed" or item.processing_date is None:
                continue
            later = [d for d in published if d > item.processing_date]
            if later:
                item.severity = "warning"
                item.title += f" (self-healing: the run that published {min(later)} should have filled it)"
                item.context["self_healed_by"] = min(later).isoformat()

    def _complete_run_findings(self, status, runs_by_job, start):
        """Silent problems in runs that completed: unusual duration, source errors swallowed by the processor."""
        findings = []
        for job_id in status.job_ids:
            offset = self.inventory().jobs[job_id].offset if job_id in self.inventory().jobs else 0
            history = [r for r in runs_by_job.get(job_id, []) if r.status == "Complete" and not is_backfill(r, offset)]
            durations = {}
            for run in history:
                diagnosis = self.run_diagnosis(run) if self.archive.has(run) else None
                if diagnosis:
                    durations[run.id] = (run, diagnosis)
            for run, diagnosis in durations.values():
                if run.started < start:
                    continue
                others = [d.duration_s for r, d in durations.values() if r.id != run.id and d.duration_s]
                if len(others) >= self.settings.baseline_min_runs and diagnosis.duration_s is not None:
                    median = statistics.median(others)
                    if (median >= 60 and diagnosis.duration_s < median * 0.2) or \
                            (diagnosis.duration_s > 300 and diagnosis.duration_s > median * 5):
                        findings.append(Finding(
                            "duration_anomaly", "info",
                            f"Run {run.id} took {diagnosis.duration_s:.0f}s against a usual {median:.0f}s",
                            status.key, status.dataset_ids, [job_id], run.processing_date, [run.id],
                            [f"Median of {len(others)} other complete runs of job {job_id}: {median:.0f}s"]))
                if diagnosis.category.startswith("source_"):
                    findings.append(Finding(
                        "completed_with_errors", "info",
                        f"Run {run.id} completed but logged {diagnosis.category.replace('_', ' ')}",
                        status.key, status.dataset_ids, [job_id], run.processing_date, [run.id],
                        diagnosis.evidence[:3], diagnosis))
        return findings
