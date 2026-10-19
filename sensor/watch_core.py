"""DatasetWatch sensor core: reads the published data folder and reports facts, never verdicts.

Plain Python on purpose (no AlgorithmImports), so it runs under pytest against a fake data folder and inside a
QuantConnect cloud backtest against the real /Data. QuantConnect rejects project files that import compression or
encoding packages, so zip archives are read through a reader the caller injects (LEAN's Compression in the cloud)
and the output is plain JSON text. The monitor (dsmon) keeps the history and does the judging:
a sensor that decides "stale" or "thin" on its own has no memory of yesterday to decide it with.
"""
import json
import math
import os
import re
import time
from collections import Counter

FORMAT_VERSION = 1
DATA_EXTENSIONS = (".csv", ".zip", ".tsv", ".json")
_DATE_NAME = re.compile(r"^(\d{8})(?:[_.\-]|$)")
_ROW_DATE = re.compile(r"^(\d{8})")
_SECURITY = re.compile(r"^[A-Z0-9.\-]{1,20} [A-Z0-9]{12}$")
_MISSING = {"", "nan", "NaN", "None", "null", "inf", "-inf"}
# Liquid tickers that nearly every per-ticker dataset covers. They are always profiled on top of the newest files:
# a processor that rewrites every file in one go leaves equal modification times, and a sample by time alone can
# then miss every file that got the new date (FINRA monthly showed "missing August" while aapl.csv had it).
CANARIES = ("aapl", "msft", "spy", "qqq", "ibm", "jpm")


class Walker:
    """Incremental version of find_dataset_dirs: walk() does at most `budget` seconds of work per call, so the
    cloud backtest can spread discovery over several time steps (Initialize has a hard 5-minute limit)."""

    def __init__(self, root, roots, max_depth=4, max_dirs=20000):
        self.found, self.visited, self.max_depth, self.max_dirs = [], 0, max_depth, max_dirs
        self.stack = [(os.path.join(root, top), 0) for top in reversed(roots) if os.path.isdir(os.path.join(root, top))]

    @property
    def done(self):
        return not self.stack or self.visited >= self.max_dirs

    def walk(self, budget):
        deadline = time.time() + budget
        while self.stack and self.visited < self.max_dirs and time.time() < deadline:
            path, depth = self.stack.pop()
            self.visited += 1
            try:
                entries = list(os.scandir(path))
            except OSError:
                continue
            files = 0
            subdirs = []
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    subdirs.append(entry.path)
                elif entry.name.endswith(DATA_EXTENSIONS):
                    files += 1
            if files:
                self.found.append(path)
            # Per-entity folders hold thousands of files and no sub-datasets, except a few named ones (universe).
            # Folders with thousands of subfolders are per-entity trees: walking them would spend the budget.
            if depth < self.max_depth and (len(subdirs) <= 20 or (files < 500 and len(subdirs) < 2000)):
                self.stack.extend((sub, depth + 1) for sub in sorted(subdirs, reverse=True))
        return self.done


def find_dataset_dirs(root, roots, max_depth=4, max_dirs=20000):
    """Directories that hold data files directly, under each configured root (e.g. 'alternative')."""
    found, visited = [], 0
    for top in roots:
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        stack = [(base, 0)]
        while stack and visited < max_dirs:
            path, depth = stack.pop()
            visited += 1
            try:
                entries = list(os.scandir(path))
            except OSError:
                continue
            files = [e for e in entries if e.is_file() and e.name.endswith(DATA_EXTENSIONS)]
            if files:
                found.append(path)
            if depth < max_depth:
                # A folder of per-ticker files has no sub-datasets; only descend from folders without many files.
                if len(files) < 500:
                    stack.extend((e.path, depth + 1) for e in entries if e.is_dir() and not e.name.startswith("."))
    return sorted(found), visited


MAX_BYTES = 32 * 1024 * 1024


class NotRead(Exception):
    """A file the sensor skipped on purpose (no zip reader, or too large); not a data problem."""


class Partial(list):
    """Rows of a file read only up to MAX_BYTES: the last row may be cut and later rows are missing."""
    partial = True


MAX_ZIP_BYTES = 64 * 1024 * 1024


def read_rows(path, limit=None, max_bytes=MAX_BYTES, zip_reader=None):
    """Data rows of a csv/tsv/json-lines file, or of the first member of a zip, up to max_bytes.
    zip_reader(path) -> list of lines; without one, zip archives raise NotRead."""
    if path.endswith(".zip"):
        if zip_reader is None:
            raise NotRead("no zip reader")
        if os.path.getsize(path) > MAX_ZIP_BYTES:
            raise NotRead("zip too large to read")
        try:
            lines = [str(line) for line in zip_reader(path)]
        except Exception as error:
            raise ValueError(f"unreadable zip: {error}")
        rows = [line for line in lines if line.strip()]
        return rows[:limit] if limit else rows
    try:
        with open(path, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError as error:
        raise ValueError(f"unreadable: {error}")
    cut = len(raw) > max_bytes
    rows = [line for line in raw[:max_bytes].decode("utf-8", errors="replace").splitlines() if line.strip()]
    if cut:
        rows = Partial(rows[:-1])
    return rows[:limit] if limit else rows


def tail_date(path, size=65536):
    """Date of the last row of a large plain file, without reading all of it (None for zips)."""
    if path.endswith(".zip"):
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, os.path.getsize(path) - size))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        match = _ROW_DATE.match(line)
        if match:
            return match.group(1)
    return None


def row_profile(rows):
    """Facts about one per-entity file: dates, order, duplicates, malformed rows, missing values."""
    dates, bad, missing = [], 0, 0
    widths = Counter(len(r.split(",")) for r in rows[:200])
    width = widths.most_common(1)[0][0] if widths else 0
    for row in rows:
        parts = row.split(",")
        if width and len(parts) != width:
            bad += 1
        missing += sum(1 for p in parts[1:] if p.strip() in _MISSING)
        match = _ROW_DATE.match(row)
        if match:
            dates.append(match.group(1))
    header = bool(rows) and not _ROW_DATE.match(rows[0])
    unsorted = sum(1 for a, b in zip(dates, dates[1:]) if b < a)
    duplicates = len(dates) - len(set(dates))
    return {"rows": len(rows), "partial": getattr(rows, "partial", False),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "unsorted": unsorted, "dups": duplicates, "bad": bad, "missing": missing, "header": header,
            "width": width}


def universe_profile(rows):
    """Facts about one dated file: row count and, when it is an equity universe, identity collisions."""
    facts = {"rows": len(rows)}
    if rows and _SECURITY.match(rows[0].split(",")[0]):
        claims = {}
        for row in rows:
            parts = row.split(",")
            if len(parts) > 1:
                claims.setdefault(parts[1].upper(), set()).add(parts[0])
        collisions = sorted(t for t, ids in claims.items() if len(ids) > 1)
        facts["collisions"] = len(collisions)
        if collisions:
            facts["collision_examples"] = collisions[:3]
    return facts


def scan_dataset(path, root, recent_dated=45, sample_files=40, deadline=None, zip_reader=None):
    """Facts about one dataset directory. Dated files ('20261013.csv') are profiled newest first; per-entity
    files are sampled by modification time, since the newest writes are where today's damage lands, plus the
    CANARIES tickers when the directory has them."""
    entries = [e for e in os.scandir(path) if e.is_file() and e.name.endswith(DATA_EXTENSIONS)]
    facts = {"p": os.path.relpath(path, root).replace(os.sep, "/"), "n": len(entries),
             "b": sum(e.stat().st_size for e in entries),
             "mt": int(max((e.stat().st_mtime for e in entries), default=0))}
    dated = sorted(((m.group(1), e) for e in entries for m in [_DATE_NAME.match(e.name)] if m), key=lambda x: x[0])
    others = [e for e in entries if not _DATE_NAME.match(e.name)]
    if dated:
        recent = []
        for stamp, entry in dated[-recent_dated:]:
            if deadline and time.time() > deadline:
                facts["truncated"] = True
                break
            item = {"d": stamp, "mt": int(entry.stat().st_mtime), "sz": entry.stat().st_size}
            try:
                item.update(universe_profile(read_rows(entry.path, zip_reader=zip_reader)))
            except NotRead as skipped:
                item["skipped"] = str(skipped)
            except ValueError as error:
                item["err"] = str(error)[:120]
            recent.append(item)
        facts["dated"] = {"count": len(dated), "first": dated[0][0], "newest": dated[-1][0], "recent": recent}
    if others:
        sample = sorted(others, key=lambda e: e.stat().st_mtime, reverse=True)[:sample_files]
        chosen = {e.name for e in sample}
        sample += [e for e in others if e.name.split(".", 1)[0].lower() in CANARIES and e.name not in chosen]
        profiles, errors, skipped = [], [], 0
        for entry in sample:
            if deadline and time.time() > deadline:
                facts["truncated"] = True
                break
            try:
                profile = row_profile(read_rows(entry.path, zip_reader=zip_reader))
            except NotRead:
                skipped += 1
                continue
            except ValueError as error:
                errors.append(f"{entry.name}: {error}"[:120])
                continue
            if profile["partial"]:
                profile["last"] = tail_date(entry.path) or profile["last"]
            profile["f"] = entry.name
            profile["mt"] = int(entry.stat().st_mtime)
            profiles.append(profile)
        lasts = [p["last"] for p in profiles if p["last"]]
        newest = max(lasts) if lasts else None
        facts["files"] = {
            "sampled": len(profiles), "newest_last": newest,
            "at_newest": sum(1 for p in profiles if newest and p["last"] == newest),
            "unsorted": sum(p["unsorted"] for p in profiles), "dups": sum(p["dups"] for p in profiles),
            "bad": sum(p["bad"] for p in profiles), "missing": sum(p["missing"] for p in profiles),
            "rows": sum(p["rows"] for p in profiles), "errors": errors[:5],
            "partial": sum(1 for p in profiles if p["partial"]), "skipped": skipped,
            "worst": sorted(({k: p[k] for k in ("f", "unsorted", "dups", "bad", "rows", "last")} for p in profiles
                             if p["unsorted"] or p["dups"] or p["bad"]),
                            key=lambda p: -(p["unsorted"] + p["dups"] + p["bad"]))[:5],
        }
    return facts


def peek(root, relpath, limit=60):
    """Immediate children of a folder (names, data-file count): a cheap view of what exists where."""
    path = os.path.join(root, relpath)
    if not os.path.isdir(path):
        return {"missing": True}
    names, files = [], 0
    for entry in os.scandir(path):
        if entry.is_dir():
            names.append(entry.name)
        elif entry.name.endswith(DATA_EXTENSIONS):
            files += 1
    return {"dirs": len(names), "files": files, "names": sorted(names)[:limit]}


def tail_facts(root, relpath, rows=5, zip_reader=None):
    """Row count and last rows of one file, for the monitor to compare against the source."""
    path = os.path.join(root, relpath)
    if not os.path.isfile(path):
        return {"missing": True}
    try:
        lines = read_rows(path, zip_reader=zip_reader)
    except (ValueError, NotRead) as error:
        return {"err": str(error)[:200]}
    return {"rows": len(lines), "partial": getattr(lines, "partial", False), "tail": lines[-rows:],
            "mt": int(os.path.getmtime(path))}


def encode(payload, chunk_chars=3000):
    """Compact JSON split into numbered chunks small enough for the per-day log quota."""
    text = json.dumps(payload, separators=(",", ":"))
    total = max(1, math.ceil(len(text) / chunk_chars))
    return [f"DSW|{FORMAT_VERSION}|{i + 1}/{total}|{text[i * chunk_chars:(i + 1) * chunk_chars]}" for i in range(total)]


def decode(lines):
    """Inverse of encode(), from log lines in any order; raises ValueError while chunks are missing."""
    parts, total = {}, None
    for line in lines:
        index = line.find("DSW|")
        if index < 0:
            continue
        _, version, position, data = line[index:].rstrip("\r\n").split("|", 3)
        number, count = (int(x) for x in position.split("/"))
        parts[number], total = data, count
    if not total or len(parts) != total:
        raise ValueError(f"have {len(parts)} of {total} chunks")
    return json.loads("".join(parts[i] for i in range(1, total + 1)))
