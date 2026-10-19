"""Job log archive and failure diagnosis.

Run history lasts 14 days but the log objects behind it expire after about 7, so every log is archived
locally the first time it is seen.
"""
import gzip
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .api import fetch_text

# Lines that look like errors but are not: Jupyter routes all cell output through ERROR::, and the
# JobProcessor logs harmless assembly-loading failures on every start.
_NOISE = re.compile(r"Composer\.LoadPartsSafely|Executing Cell|Ending Cell|Input Notebook|Output Notebook|"
                    r"Working directory|IPKernelApp|Executing notebook with kernel|CompositeLogHandler|"
                    r"Initializer\.Start|Composer\.cs|Initializer\.cs")
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z\s+(TRACE|ERROR|DEBUG)::\s?(.*)$")
_PY_EXCEPTION = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|error_perm|error_temp))(?::\s*(.*))?$")
_NET_EXCEPTION = re.compile(r"(?:Unhandled exception\.\s*)?((?:System|QuantConnect)[\w.]*Exception):\s*(.*)")
_URL = re.compile(r"https?://[^\s\"'<>)\]]+")
_HTTP_STATUS = [
    re.compile(r"status code does not indicate success:\s*(\d{3})"),
    re.compile(r"HTTP Error (\d{3})"),
    re.compile(r"\b(\d{3}) (?:Client|Server) Error"),
    re.compile(r"Response \[(\d{3})\]"),
    re.compile(r"returned error:\s*(\d{3})"),
    re.compile(r"\bstatus[ =:]+(\d{3})\b"),
]


@dataclass
class Diagnosis:
    category: str
    summary: str  # the root error line
    http_status: int = None
    urls: list = field(default_factory=list)
    evidence: list = field(default_factory=list)  # the lines that decided the category
    duration_s: float = None
    lines: int = 0


# Bump when the rules change, so cached diagnoses get recomputed.
DIAGNOSIS_VERSION = 1

# (category, pattern) in priority order: the first category with a hit wins.
_RULES = [
    ("source_bad_content", r"null bytes|holds no release|empty (?:body|response)|BadZipFile|not a zip file|"
                           r"Unexpected character encountered while parsing|JSONDecodeError|XMLSyntaxError|"
                           r"no tables found|ParserError"),
    ("source_tls", r"[Cc]ertificate verification|CERTIFICATE_VERIFY_FAILED|SSL(?:Error|:)|"
                   r"certificate common name doesn't match|AuthenticationException: The remote certificate"),
    ("source_access_denied", r"\b40[13]\b.*(?:Forbidden|Unauthorized|denied|error)|(?:Forbidden|Unauthorized)\b|"
                             r"Access Denied|Login failed|\b530 Please|returned error: 40[13]|API key"),
    ("source_not_published", r"\b404\b.*(?:Not Found|error)|Not Found\b|error_perm: 550|550 Failed to open|"
                             r"Failed to find .* in the index|No such file or directory: 'https?:|"
                             r"[Nn]o data (?:available|found|returned) for"),
    ("source_unavailable", r"\b50[0234]\b.*(?:Error|Gateway|Unavailable|error)|Bad Gateway|Service Unavailable|"
                           r"Gateway Time-?out|HttpClient\.Timeout|timed? ?out|ReadTimeout|ConnectTimeout|"
                           r"Connection (?:reset|refused|aborted)|RemoteDisconnected|Max retries exceeded|"
                           r"Temporary failure in name resolution|Name or service not known|No route to host|"
                           r"TaskCanceledException"),
    ("missing_input", r"DirectoryNotFoundException|FileNotFoundException|No such file or directory: '/|"
                      r"Raw data /raw|no [\w ]*(?:constituents|input|raw) file|Found no symbols|"
                      r"[Mm]issing (?:input|raw|source) file|does not exist: /"),
    ("infra", r"OutOfMemory|MemoryError|Killed\b|No space left on device|exit code 137|"
              r"Cannot allocate memory|SIGKILL|Segmentation fault"),
]
_COMPILED = [(name, re.compile(pattern)) for name, pattern in _RULES]


def parse_lines(text):
    """(timestamp, level, message) per log line; continuation lines keep the previous stamp."""
    parsed, last = [], (None, "TRACE")
    for raw in text.splitlines():
        match = _STAMP.match(raw)
        if match:
            last = (match.group(1), match.group(2))
            parsed.append((last[0], last[1], match.group(3)))
        elif raw.strip():
            parsed.append((last[0], last[1], raw.strip()))
    return parsed


def signal_lines(text):
    """Log lines with the start-up noise removed."""
    return [(stamp, level, message) for stamp, level, message in parse_lines(text) if not _NOISE.search(message)]


def diagnose(text, exit_code=None, status=None):
    lines = parse_lines(text)
    signal = [m for _, _, m in lines if not _NOISE.search(m)]
    duration = _duration(lines)
    root = _root_error(signal)

    category, evidence = None, []
    # Rules look at error-looking lines first, so a TRACE line that merely mentions "timeout" in a
    # config dump cannot decide the category of a run that failed for another reason.
    errorish = [m for m in signal if re.search(r"(?i)error|exception|fail|denied|forbidden|warning|traceback|"
                                               r"unable|could not|cannot|no such|not found|null bytes|holds no|"
                                               r"found no|missing|\bno [\w ]+ file", m)]
    for name, pattern in _COMPILED:
        hits = [m for m in errorish if pattern.search(m)]
        if hits:
            category, evidence = name, hits[:5]
            break
    if category is None and exit_code in (137, 139):
        category = "infra"
    if category is None:
        if status == "Failed" or (exit_code not in (None, 0)):
            category = "code_error" if root else "unknown"
        else:
            category = "ok"

    http_status = None
    for line in evidence + signal[::-1]:
        for pattern in _HTTP_STATUS:
            match = pattern.search(line)
            if match and match.group(1) != "200":
                http_status = int(match.group(1))
                break
        if http_status:
            break

    urls = []
    for message in evidence + ([root] if root else []):
        for url in _URL.findall(message):
            url = sanitize_url(url)
            if url and url not in urls and "*" not in url:
                urls.append(url)
    return Diagnosis(category, root or (evidence[0] if evidence else ""), http_status, urls, evidence,
                     duration, len(lines))


def sanitize_url(url):
    """Drop query strings and fragments: they can carry API keys and signatures."""
    url = url.rstrip(".,;:'\"")
    return re.split(r"[?#]", url, 1)[0]


def _root_error(signal):
    for message in reversed(signal):
        match = _PY_EXCEPTION.match(message)
        if match and not message.startswith(("File ", "raise ", "--->")):
            return message
    for message in signal:
        match = _NET_EXCEPTION.search(message)
        if match:
            return message
    for message in reversed(signal):
        if re.search(r"(?i)\berror\b|exception|failed", message) and "exit code" not in message:
            return message
    return None


def _duration(lines):
    stamps = [s for s, _, _ in lines if s]
    if len(stamps) < 2:
        return None
    first = datetime.fromisoformat(stamps[0])
    last = datetime.fromisoformat(stamps[-1])
    return (last - first).total_seconds()


@dataclass
class SyncInfo:
    """What one node's sync log says.

    decision: "first" (no prior sync of the date), "again" (a prior sync exists but this is a new turn of the
    syncing job's cron: the date is published a second time), "skipped" (already synchronized), "forced" (no
    ShouldSync step: a Manual Run with Synchronize Always), or "unknown".
    """
    decision: str
    reason: str = ""
    prior_sync_at: datetime = None  # "last successful sync" the decision refers to
    started_at: datetime = None
    synced_at: datetime = None  # data rsynced into /Data
    tar: str = None
    error: str = None


_SYNC_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_LAST_SYNC = re.compile(r"last successful sync: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def describe_sync(sync, info):
    """One line per node: status, the ShouldSync decision and when the data reached /Data."""
    if info is None:
        return f"{sync.name}: {sync.status} (sync log not available)"
    parts = [f"{sync.name}: {sync.status}, decision {info.decision}"]
    if info.synced_at:
        parts.append(f"data in /Data at {info.synced_at:%Y-%m-%d %H:%M:%S} UTC")
    if info.prior_sync_at:
        parts.append(f"prior sync of the date at {info.prior_sync_at:%Y-%m-%d %H:%M} UTC")
    if info.tar:
        parts.append(info.tar)
    if info.error:
        parts.append(f"error: {info.error[:200]}")
    return "; ".join(parts)


def parse_sync_log(text):
    lines = text.splitlines()

    def stamp(line):
        match = _SYNC_STAMP.match(line)
        return datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone.utc) if match else None

    decision_line = next((l for l in lines if "ShouldSync()" in l), None)
    reason = decision_line.split("ShouldSync(): ", 1)[-1].strip() if decision_line else ""
    if decision_line is None:
        decision = "forced" if any("start sync for job" in l for l in lines) else "unknown"
    elif "will not be synced" in reason:
        decision = "skipped"
    elif "No prior successful sync" in reason:
        decision = "first"
    elif "will be synced" in reason:
        decision = "again"
    else:
        decision = "unknown"
    prior = _LAST_SYNC.search(reason)
    info = SyncInfo(decision, reason,
                    datetime.strptime(prior.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if prior else None,
                    stamp(lines[0]) if lines else None)
    for line in lines:
        if "Successfully synced" in line or "sync finished successfully" in line:
            info.synced_at = info.synced_at or stamp(line)
        tar = re.search(r"/([^/\s]+\.tar) downloaded", line)
        if tar:
            info.tar = tar.group(1)
        if info.error is None and re.search(r"(?i)\b(error|exception|failed)\b", line) \
                and "UpdateSyncConsumer" not in line:
            info.error = line.split("::", 1)[-1].strip()
    return info


class LogArchive:
    """Job and sync logs on disk, plus the bookkeeping that keeps every check incremental:

    - a log is downloaded once; a URL that failed (expired, 403) is remembered and not retried,
    - a run's diagnosis is cached next to its log, so a check only diagnoses runs it has not seen.
    """

    def __init__(self, root):
        self.root = Path(root)

    def path(self, run):
        return self.root / str(run.job_id) / f"{run.id}.log.gz"

    def _missing_path(self, path):
        return path.with_name(path.name.replace(".log.gz", ".missing"))

    def has(self, run):
        return self.path(run).exists()

    def known_missing(self, run):
        return self._missing_path(self.path(run)).exists()

    def read(self, run):
        path = self.path(run)
        if path.exists():
            return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        return None

    def _download(self, path, url):
        if path.exists():
            return gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        missing = self._missing_path(path)
        if missing.exists() or not url:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = fetch_text(url)
        except Exception as error:
            # Expired presigned URLs answer 403 forever; transient errors get retried on the next check.
            if "403" in str(error) or "404" in str(error):
                missing.write_text(str(error)[:200], encoding="utf-8")
            return None
        path.write_bytes(gzip.compress(text.encode("utf-8")))
        return text

    def fetch_sync(self, run, sync):
        """A node's sync log, archived like the job log."""
        if sync.status in (None, "In Progress", "Queued"):
            return None
        path = self.root / str(run.job_id) / f"{run.id}.sync-{re.sub(r'[^A-Za-z0-9]+', '', sync.name)}.log.gz"
        return self._download(path, sync.url)

    def fetch(self, run):
        """Archived text, downloading it first when the presigned URL is still live."""
        if run.status in (None, "In Progress"):
            return None
        return self._download(self.path(run), run.log_url)

    def cached_diagnosis(self, run):
        path = self.path(run).with_name(f"{run.id}.diag.json")
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Diagnosis(**data) if data.get("version") is None or data.pop("version") == DIAGNOSIS_VERSION else None

    def store_diagnosis(self, run, diagnosis):
        path = self.path(run).with_name(f"{run.id}.diag.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(diagnosis)
        data["version"] = DIAGNOSIS_VERSION
        path.write_text(json.dumps(data), encoding="utf-8")
