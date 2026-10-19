"""Datasets, their data jobs, and how the jobs pair up into pipelines (primary + redundancy)."""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

# Job configuration holds plaintext vendor secrets. Only these keys keep their values.
_SAFE_CONFIG_VALUES = {"qc-datafleet-alert-emails", "dataset-name"}


@dataclass
class ChainStep:
    name: str
    command: str
    synchronize: int
    enabled: bool


@dataclass
class Job:
    id: int
    name: str
    enabled: bool
    schedule: str
    timezone: str
    offset: int
    queue: str  # "primary", "redundancy" or "batch"
    commit_url: str
    timeout_minutes: int
    chain: list
    dataset_ids: list = field(default_factory=list)
    alert_emails: list = field(default_factory=list)
    config: dict = field(default_factory=dict)  # key -> value, secrets replaced by "*"

    @property
    def commit(self):
        match = re.search(r"/commit/([0-9a-f]{7,40})", self.commit_url or "")
        return match.group(1) if match else None

    @property
    def repo(self):
        match = re.search(r"github\.com/([^/]+/[^/]+)/commit/", self.commit_url or "")
        return match.group(1) if match else None


@dataclass
class Dataset:
    id: int
    name: str
    vendor: str
    slug: str
    job_ids: list


@dataclass
class Pipeline:
    """Jobs that produce the same output: one primary and its redundancy runs."""
    key: str
    dataset_ids: list
    job_ids: list

    def jobs(self, inventory):
        return [inventory.jobs[i] for i in self.job_ids]


@dataclass
class Inventory:
    fetched_at: str
    datasets: dict  # id -> Dataset
    jobs: dict  # id -> Job
    job_names: dict  # every data job id -> name, including jobs that belong to no dataset

    def pipelines(self, dataset_ids=None):
        wanted = set(dataset_ids) if dataset_ids is not None else None
        candidates = [j for j in self.jobs.values()
                      if j.queue != "batch" and (wanted is None or wanted.intersection(j.dataset_ids))]
        groups = {}
        for job in candidates:
            if job.queue == "primary":
                groups.setdefault(pipeline_key(job), []).append(job)
        for job in candidates:
            if job.queue != "primary":
                groups.setdefault(self._partner_key(job, groups), []).append(job)
        pipelines = []
        for key, jobs in sorted(groups.items()):
            jobs.sort(key=lambda j: (j.queue != "primary", not j.enabled, j.id))
            dataset_ids = sorted({d for j in jobs for d in j.dataset_ids})
            pipelines.append(Pipeline(key, dataset_ids, [j.id for j in jobs]))
        return pipelines

    @staticmethod
    def _partner_key(job, groups):
        """The primary a redundancy backs up. Names are not reliable ('Alternative U.S. Equity SEC' is backed by
        'Redundancy Alternative Equity SEC'), so candidates must share a dataset and the best one shares the
        most chain commands, then chain step names, then name words."""
        key = pipeline_key(job)
        if key in groups:
            return key
        primaries = [(k, p) for k, jobs in groups.items() for p in jobs
                     if p.queue == "primary" and set(p.dataset_ids) & set(job.dataset_ids)]
        if not primaries:
            return key

        def score(primary):
            commands = {s.command for s in job.chain if s.command} & {s.command for s in primary.chain if s.command}
            steps = {_normalize(s.name) for s in job.chain} & {_normalize(s.name) for s in primary.chain}
            words = set(pipeline_key(job).split()) & set(pipeline_key(primary).split())
            return len(commands), len(steps), len(words), set(primary.dataset_ids) == set(job.dataset_ids)

        best_key, best = max(primaries, key=lambda kp: score(kp[1]))
        return best_key if any(score(best)[:3]) or len(primaries) == 1 else key

    def find_datasets(self, query):
        """Dataset ids by id, slug or a case-insensitive name fragment."""
        text = str(query).strip().lower()
        if text.isdigit() and int(text) in self.datasets:
            return [int(text)]
        return [d.id for d in self.datasets.values() if text in d.name.lower() or text == d.slug]

    def to_json(self):
        return {
            "fetched_at": self.fetched_at,
            "datasets": {k: asdict(v) for k, v in self.datasets.items()},
            "jobs": {k: asdict(v) for k, v in self.jobs.items()},
            "job_names": self.job_names,
        }

    @classmethod
    def from_json(cls, data):
        jobs = {}
        for key, value in data["jobs"].items():
            value = dict(value)
            value["chain"] = [ChainStep(**step) for step in value["chain"]]
            jobs[int(key)] = Job(**value)
        datasets = {int(k): Dataset(**v) for k, v in data["datasets"].items()}
        return cls(data["fetched_at"], datasets, jobs, {int(k): v for k, v in data["job_names"].items()})


def pipeline_key(job):
    """Normalized job name: 'Redundancy Alternative US Treasury Yield Curve' -> 'us treasury yield curve'."""
    return _normalize(job.name)


def _normalize(name):
    name = name.lower().replace("u.s.", "us")
    name = re.sub(r"\b(redundancy|alternative|vendor|repo)\b", " ", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def fetch_inventory(api, workers=6):
    job_names = {j["id"]: j["name"] for j in api.post_form("/market/dashboard/data-job/list/")["list"]}
    market = api.post_json("/market/data/list", {"organizationId": api.organization_id})["list"]

    def read(dataset):
        return dataset, api.post_json("/market/sections/read", {"id": dataset["id"], "organizationId": api.organization_id})

    datasets, jobs = {}, {}
    with ThreadPoolExecutor(workers) as pool:
        for dataset, sections in pool.map(read, market):
            records = (sections.get("sections", {}).get("dashboard") or {}).get("jobs") or []
            datasets[dataset["id"]] = Dataset(dataset["id"], dataset["name"].strip(), dataset.get("vendorName", ""),
                                              dataset.get("url", ""), sorted(r["jobId"] for r in records))
            for record in records:
                job = jobs.get(record["jobId"]) or _job_from_record(record)
                job.dataset_ids = sorted(set(job.dataset_ids) | {dataset["id"]})
                jobs[job.id] = job
    return Inventory(datetime.now(timezone.utc).isoformat(timespec="seconds"), datasets, jobs, job_names)


def _job_from_record(record):
    config, emails = {}, []
    for item in record.get("configuration") or []:
        name = item.get("name", "")
        value = item.get("value")
        config[name] = value if name in _SAFE_CONFIG_VALUES else "*"
        if name == "qc-datafleet-alert-emails" and value:
            try:
                emails = json.loads(value)
            except json.JSONDecodeError:
                emails = re.findall(r"[\w.+-]+@[\w.-]+", value)
    queue = record.get("queueNamePrefix") or "primary"
    chain = [ChainStep(s.get("name", ""), s.get("command") or "", int(s.get("synchronize") or 0), bool(s.get("enabled")))
             for s in sorted(record.get("jobChain") or [], key=lambda s: s.get("order", 0))]
    return Job(
        id=record["jobId"], name=record["name"].strip(), enabled=record.get("status") == "Enabled",
        schedule=record.get("schedule") or "", timezone=record.get("timezone") or "UTC",
        offset=int(record.get("offset") or 0), queue=queue, commit_url=record.get("commitUrl") or "",
        timeout_minutes=_minutes(record.get("timeout")), chain=chain,
        alert_emails=emails, config=config)


def _minutes(timeout):
    """Job timeout as minutes; the API sends 'HH:MM:SS' or null."""
    if not timeout:
        return 0
    parts = [int(p) for p in str(timeout).split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-3:]
    return hours * 60 + minutes + (1 if seconds else 0)
