"""Judge what the DatasetWatch sensor saw in the published data folder.

The sensor reports facts per data directory; this module keeps every snapshot and compares the newest one with
the ones before it. Most data problems only show as a change: a duplicate row is normal in a dataset with several
rows per date and alarming in one that never had any, so the baseline is the directory's own history.
"""
import json
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .health import Finding
from .calendars import expected_days
from .holidays import market_closed_reason, nyse_holidays, us_federal_holidays

# Directories that are not datasets: processor caches and leftovers the publish step copied verbatim.
IGNORED_PARTS = ("/cache", ".stale", "/tmp", "/.")


class SnapshotStore:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def save(self, payload):
        at = datetime.fromtimestamp(payload.get("at", 0), timezone.utc)
        path = self.folder / f"{at:%Y%m%dT%H%M%S}.json"
        path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        return path

    def history(self, limit=30):
        """Snapshots oldest first."""
        files = sorted(self.folder.glob("*.json"))[-limit:]
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def _day(text):
    return datetime.strptime(text, "%Y%m%d").date()


def _by_path(payload):
    return {d["p"]: d for d in payload.get("datasets", []) if "p" in d}


def dataset_for(path, mapping):
    """Dataset ids whose configured path prefixes cover this directory."""
    return sorted({ds for ds, prefixes in mapping.items() for prefix in prefixes if path.startswith(prefix)})


def judge(current, history, mapping=None, today=None, only_mapped=False, max_age=None, ignore=(), calendars=None):
    """Findings for the newest snapshot, against the snapshots before it (oldest first, current excluded)."""
    mapping = mapping or {}
    max_age = max_age or {}
    calendars = calendars or {}
    today = today or datetime.now(timezone.utc).date()
    previous = _by_path(history[-1]) if history else {}
    findings = []
    for path, facts in sorted(_by_path(current).items()):
        if any(part in path for part in IGNORED_PARTS) or any(path.startswith(i) for i in ignore):
            continue
        datasets = dataset_for(path, mapping)
        if only_mapped and not datasets:
            continue

        def finding(kind, severity, title, detail, day=None):
            item = Finding(kind, severity, title, path, datasets, processing_date=day, detail=detail)
            item.context["content_path"] = path
            findings.append(item)

        if facts.get("err"):
            finding("content_unreadable", "warning", "The sensor could not read this directory", [facts["err"]])
            continue
        if "dated" in facts:
            _judge_dated(finding, facts["dated"], today, _longest(calendars, path))
        if "files" in facts:
            _judge_files(finding, facts["files"], previous.get(path, {}).get("files"))
        _judge_progress(finding, path, facts, history, _longest(calendars, path), today)
        _judge_age(finding, path, facts, max_age, today)
    return findings


def _judge_age(finding, path, facts, max_age, today):
    """Newest date older than the configured limit for this directory (cadence plus publication lag)."""
    limits = [(prefix, days) for prefix, days in max_age.items() if path.startswith(prefix)]
    if not limits:
        return
    prefix, days = max(limits, key=lambda item: len(item[0]))
    newest = (facts.get("dated") or {}).get("newest") or (facts.get("files") or {}).get("newest_last")
    if not newest:
        return
    age = (today - _day(newest)).days
    if age > days:
        finding("content_too_old", "critical", f"Newest date is {newest}, {age} days old (limit {days})",
                [f"Limit for '{prefix}': the dataset's cadence plus its source's usual publication lag. Either the "
                 "processor stopped writing new dates or the source stopped publishing."], _day(newest))


def _cadence(days):
    gaps = [(b - a).days for a, b in zip(days, days[1:]) if b > a]
    return statistics.median(gaps) if gaps else None


def _longest(by_prefix, path):
    matches = [(p, v) for p, v in by_prefix.items() if path.startswith(p)]
    return max(matches, key=lambda item: len(item[0]))[1] if matches else None


def _judge_dated(finding, dated, today, calendar=None):
    recent = dated.get("recent", [])
    days = sorted({_day(r["d"]) for r in recent if r.get("d")})
    if len(days) < 5:
        return
    cadence = _cadence(days)
    # Which weekdays this directory writes, read from its own files: Mon-Fri for most, Tue-Sat for datasets stamped
    # the day after the session. A weekday it uses in under half of the weeks is not expected (one odd Saturday
    # stamp must not turn every weekend into a gap).
    weeks = max(1, len({d.isocalendar()[:2] for d in days}))
    usage = {w: sum(1 for d in days if d.weekday() == w) for w in range(7)}
    weekdays = {w for w, n in usage.items() if n >= 0.5 * weeks}
    closed = set()
    for year in {d.year for d in days} | {today.year}:
        closed |= set(nyse_holidays(year)) | set(us_federal_holidays(year))

    # A declared calendar (config.toml) decides the expected days; otherwise they are inferred from the files.
    if calendar is not None:
        expected = expected_days(calendar, max(days[0], today - timedelta(days=30)), days[-1])
        missing = [d for d in (expected or []) if d not in set(days)]
        if missing:
            finding("content_missing_dates", "warning",
                    f"{len(missing)} day(s) without a file in the last 30 days ({calendar} calendar)",
                    [", ".join(d.isoformat() for d in missing[:12]),
                     "Every other expected day has its own file: a missing one is a date LEAN serves nothing for "
                     "(a universe selection that never happens, live or backtest)."], missing[-1])
    elif cadence is not None and cadence <= 1.5 and weekdays:
        start = max(days[0], today - timedelta(days=30))
        expected, cursor = [], start
        while cursor <= days[-1]:
            # A holiday removes that day's file, or the next one for datasets stamped the day after the session.
            if cursor.weekday() in weekdays and cursor not in closed and cursor - timedelta(days=1) not in closed:
                expected.append(cursor)
            cursor += timedelta(days=1)
        missing = [d for d in expected if d not in set(days)]
        if missing:
            names = "".join("MTWTFSS"[w] for w in sorted(weekdays))
            finding("content_missing_dates", "warning",
                    f"{len(missing)} day(s) without a file in the last 30 days (writes on {names})",
                    [", ".join(d.isoformat() for d in missing[:12]),
                     "Every other day of this directory has its own file: a missing one is a date LEAN serves "
                     "nothing for (a universe selection that never happens, live or backtest)."], missing[-1])
    elif cadence and len(days) >= 2 and (days[-1] - days[-2]).days > 2 * cadence + 3:
        finding("content_missing_dates", "warning",
                f"The last gap between files is {(days[-1] - days[-2]).days} days against a usual {cadence:g}",
                [f"{days[-2]} -> {days[-1]}"], days[-1])

    # Thin dates: far fewer rows than the dates around them.
    sized = [(r["d"], r["rows"]) for r in recent if isinstance(r.get("rows"), int)]
    for index, (stamp, rows) in enumerate(sized[-10:], start=max(0, len(sized) - 10)):
        around = [n for i, (_, n) in enumerate(sized) if i != index and abs(i - index) <= 10]
        if len(around) >= 5:
            # The same weekday is the baseline when there is enough of it: Factors keeps 505-row Saturday files of an
            # older build next to its 11k-row weekday files, and CNBC's Sundays hold a handful of mentions.
            weekday = _day(stamp).weekday()
            same = [n for i, (s, n) in enumerate(sized) if i != index and _day(s).weekday() == weekday]
            median = statistics.median(same) if len(same) >= 3 else statistics.median(around)
            if median >= 20 and rows < 0.25 * median:
                basis = f"a usual {median:g} on {_day(stamp):%A}s" if len(same) >= 3 else f"a usual {median:g}"
                finding("content_thin_date", "warning", f"{stamp} holds {rows} rows against {basis}",
                        ["A date with a fraction of its usual rows is a partial publication: the rest of the "
                         "entities have no data that day."], _day(stamp))

    for item in recent[-5:]:
        if item.get("collisions"):
            finding("content_identity_collision", "warning",
                    f"{item['d']}: {item['collisions']} ticker(s) claimed by more than one security",
                    [f"Examples: {', '.join(item.get('collision_examples', []))}",
                     "One ticker under two security identifiers on one date usually means a stale mapping: one of "
                     "the two rows belongs to another company."], _day(item["d"]))
        if item.get("err"):
            finding("content_unreadable", "warning", f"{item['d']}: the file could not be read", [item["err"]],
                    _day(item["d"]))


def _judge_files(finding, files, before):
    worst = [f"{w['f']}: {w['unsorted']} out of order, {w['dups']} duplicate dates, {w['bad']} malformed, "
             f"last {w['last']}" for w in files.get("worst", [])]
    if files.get("unsorted"):
        finding("content_unsorted", "warning",
                f"{files['unsorted']} row(s) out of date order in the {files['sampled']} newest files",
                worst + ["LEAN reads files forward and silently skips a row dated before the previous one: those "
                         "rows never reach an algorithm."])
    if files.get("errors"):
        finding("content_unreadable", "warning", f"{len(files['errors'])} file(s) could not be read",
                files["errors"])
    if before:
        for key, label in (("dups", "duplicate dates"), ("bad", "malformed rows")):
            now_rate = files.get(key, 0) / max(files.get("rows", 0), 1)
            then_rate = before.get(key, 0) / max(before.get("rows", 0), 1)
            if files.get(key, 0) >= 10 and now_rate > 1.5 * then_rate + 0.001:
                finding(f"content_{key}_grew", "warning",
                        f"{label.capitalize()} grew: {files[key]} in {files['rows']} rows (was {before.get(key, 0)} "
                        f"in {before.get('rows', 0)})", worst)


_PERIOD = re.compile(r"/(\d{4})(\d{2})?$")


def closed_period(path, today):
    """A folder named after a month or year that has ended (brain/sentiment/30/201608): it never gets a newer date."""
    match = _PERIOD.search(path)
    if not match:
        return False
    year, month = int(match.group(1)), int(match.group(2) or 12)
    if not 1990 <= year <= today.year + 1 or not 1 <= month <= 12:
        return False
    return (year, month) < (today.year, today.month)


def _judge_progress(finding, path, facts, history, calendar=None, today=None):
    """A directory whose newest date has not moved for several of its own cadences, across snapshots. Only dated
    folders: the newest date of a per-entity folder comes from a sample of its files and moves with the sample.
    Event-driven folders (calendar "none") and closed periods are left to the age check."""
    newest = (facts.get("dated") or {}).get("newest")
    if not newest or not history or (calendar or "").startswith("none"):
        return
    if closed_period(path, today or datetime.now(timezone.utc).date()):
        return
    seen = []
    for snapshot in history:
        old = _by_path(snapshot).get(path)
        if old:
            old_newest = (old.get("dated") or {}).get("newest")
            seen.append((datetime.fromtimestamp(snapshot.get("at", 0), timezone.utc).date(), old_newest))
    if not seen:
        return
    unchanged_since, previous = None, None
    for when, value in reversed(seen):
        if value != newest:
            previous = value
            break
        unchanged_since = when
    # Stale means it advanced while we watched and then stopped. A folder that never moved in the history is
    # historical, discontinued or slow (monthly, quarterly, event-driven): its age is _judge_age's to judge.
    if previous is None:
        return
    try:
        step = (_day(newest) - _day(previous)).days
    except (TypeError, ValueError):
        return  # not a date (an id-like stamp): nothing to measure
    dated = facts.get("dated", {})
    days = sorted({_day(r["d"]) for r in dated.get("recent", []) if r.get("d")})
    cadence = max(_cadence(days) or 1, step, 1)
    if unchanged_since:
        waited = (datetime.now(timezone.utc).date() - unchanged_since).days
        if waited > 3 * max(cadence, 1) + 3:
            finding("content_stale", "critical", f"Newest date {newest} has not moved for {waited} days",
                    [f"Usual cadence {cadence:g} day(s); the same newest date was seen in every snapshot since "
                     f"{unchanged_since}."])
