"""Compare the newest published values with the source they came from.

The sensor returns the last rows of a few configured files ("tails"); this module fetches the same observations
from the provider, from this machine, and reports where they differ. It catches what no log and no structure check
can: a processor that writes the wrong number with the right shape.
"""
import csv
import io
import json
import urllib.request
from datetime import datetime

from .health import Finding
from .probes import CONTACT_USER_AGENT


def _get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": CONTACT_USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def ecb_observations(url):
    """{date: value} from an ECB data-api csvdata response."""
    rows = csv.DictReader(io.StringIO(_get(url)))
    out = {}
    for row in rows:
        if row.get("OBS_VALUE") not in (None, ""):
            out[datetime.strptime(row["TIME_PERIOD"][:10], "%Y-%m-%d").date()] = float(row["OBS_VALUE"])
    return out


def _ours(tail, column):
    """{date: value} from our rows: column 0 is the observation date."""
    out = {}
    for row in tail:
        parts = row.split(",")
        if len(parts) > column and parts[column].strip():
            out[datetime.strptime(parts[0][:8], "%Y%m%d").date()] = float(parts[column])
    return out


def check_ecb_series(spec, tail):
    ours = _ours(tail, int(spec["column"]))
    source = ecb_observations(spec["url"])
    if not ours or not source:
        return [], f"nothing to compare (ours {len(ours)}, source {len(source)})"
    problems, revisions, compared = [], [], 0
    relative = float(spec.get("revision_tolerance", 0.01))
    for day, value in sorted(ours.items()):
        if spec.get("asof"):
            # Series published as dates of change: the value in force on `day` is the last change on or before it.
            changes = [d for d in source if d <= day]
            if not changes:
                continue
            expected = source[max(changes)]
        elif day in source:
            expected = source[day]
        else:
            continue
        compared += 1
        difference = abs(value - expected)
        if difference <= float(spec.get("tolerance", 1e-6)):
            continue
        line = f"{day}: ours {value} vs source {expected}"
        # Our datasets keep the first release; a small later difference is the source revising, not an error.
        if expected and difference / abs(expected) <= relative:
            revisions.append(line)
        else:
            problems.append(line)
    note = f"{compared} observation(s) compared"
    if revisions:
        note += f", {len(revisions)} revised by the source since we published"
    return problems, note, revisions


def check_fdic_latest_failure(spec, tail):
    """The newest failure the FDIC lists must be in our file (by certificate number, column 2)."""
    data = json.loads(_get("https://api.fdic.gov/banks/failures?sort_by=FAILDATE&sort_order=DESC&limit=3"
                           "&fields=CERT,NAME,FAILDATE"))
    latest = [d["data"] for d in data.get("data", [])]
    ours = {row.split(",")[2] for row in tail if len(row.split(",")) > 2}
    missing = [f"{d.get('NAME')} (cert {d.get('CERT')}, failed {d.get('FAILDATE')})" for d in latest[:1]
               if str(d.get("CERT")) not in ours]
    return missing, f"newest FDIC failure: {latest[0].get('NAME') if latest else '-'}", []


CHECKS = {"ecb_series": check_ecb_series, "fdic_latest_failure": check_fdic_latest_failure}


def reconcile(specs, tails):
    """Findings for every configured comparison whose tail the sensor returned."""
    findings, notes = [], []
    for spec in specs:
        path = spec["file"]
        tail = tails.get(path) or {}
        dataset = spec.get("dataset")
        name = spec.get("name", path)
        if tail.get("missing") or tail.get("err"):
            findings.append(Finding("reconcile_unavailable", "warning", f"{name}: the published file was not readable",
                                    path, [dataset] if dataset else [], detail=[json.dumps(tail)[:300]]))
            continue
        if not tail.get("tail"):
            continue
        try:
            problems, note, revisions = CHECKS[spec["check"]](spec, tail["tail"])
        except Exception as error:  # the source being down is a probe matter, not a data finding
            notes.append(f"{name}: source not reachable ({error})")
            continue
        notes.append(f"{name}: {note}")
        if revisions:
            findings.append(Finding("reconcile_revision", "info", f"{name}: the source revised published values",
                                    path, [dataset] if dataset else [],
                                    detail=revisions[:5] + ["Expected for a point-in-time dataset that keeps the "
                                                            "first release; a problem only if the dataset promises "
                                                            "revised values."]))
        if problems:
            findings.append(Finding("reconcile_mismatch", "critical", f"{name}: published values differ from the source",
                                    path, [dataset] if dataset else [],
                                    detail=problems[:8] + [f"Compared with {spec.get('url', spec['check'])}"]))
    return findings, notes
