"""Run history of the data jobs (`admin/data-processing/list`)."""
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

# Log object name: <chainId>-<uuid>-<yyyymmdd-HHMMSS>_<processing date>_<hash>_<chain name>.log
_LOG_NAME = re.compile(r"/logs/(\d+)/[\d-]+/\d+-[0-9a-f-]{36}-\d{8}-\d{6}_(\d{8})_[0-9a-f]+_([^?]+)\.log")


@dataclass
class SyncResult:
    name: str
    status: str
    url: str = None
    info: object = None  # logs.SyncInfo once the node's sync log is read


@dataclass
class Run:
    id: int
    job_id: int
    chain_id: int
    queue: str
    started: datetime  # aware UTC
    status: str  # Complete, Failed, In Progress, or None while queued
    exit_code: int
    error_message: str
    log_url: str
    sync: list = field(default_factory=list)
    processing_date: date = None
    chain_slug: str = None

    @property
    def published(self):
        """At least one node took the files from this run."""
        return any(s.status == "Complete" for s in self.sync)

    @property
    def sync_skipped(self):
        """Every node skipped: the date was already published by an earlier run of the same chain."""
        return bool(self.sync) and all(s.status == "Skipped" for s in self.sync)

    @property
    def sync_failed(self):
        return any(s.status == "Failed" for s in self.sync)


def parse_run(raw, offset=None):
    started = datetime.strptime(raw["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    processing_date, chain_slug = None, None
    match = _LOG_NAME.search(raw.get("logs") or "")
    if match:
        processing_date = datetime.strptime(match.group(2), "%Y%m%d").date()
        chain_slug = match.group(3)
    elif offset is not None:
        # DataFleet: processing date = UTC date of the run minus the job's date offset.
        processing_date = started.date() - timedelta(days=offset)
    return Run(
        id=raw["id"], job_id=raw["jobId"], chain_id=raw.get("jobChainId"), queue=raw.get("queueNamePrefix") or "primary",
        started=started, status=raw.get("status"), exit_code=raw.get("exitCode"),
        error_message=raw.get("errorMessage"), log_url=raw.get("logs"),
        sync=[SyncResult(s.get("name"), s.get("status"), s.get("url")) for s in raw.get("sync") or []],
        processing_date=processing_date, chain_slug=chain_slug)


def fetch_runs(api, job_ids, since=None, offsets=None, workers=6):
    """Runs per job, newest first. One call per job: the 250-row cap is per call, not per job."""
    offsets = offsets or {}

    def one(job_id):
        raw = api.post_form("/admin/data-processing/list/", {"jobs[]": [job_id]}).get("runs", [])
        runs = [parse_run(r, offsets.get(job_id)) for r in raw]
        if since is not None:
            runs = [r for r in runs if r.started >= since]
        return job_id, sorted(runs, key=lambda r: r.started, reverse=True), len(raw) >= 250

    result, capped = {}, []
    with ThreadPoolExecutor(workers) as pool:
        for job_id, runs, at_cap in pool.map(one, job_ids):
            result[job_id] = runs
            if at_cap:
                capped.append(job_id)
    return result, capped
