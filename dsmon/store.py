"""Finding history across checks: what is new, what got worse, what was fixed, and what was already notified."""
import json
import sqlite3
from datetime import datetime, timezone

from .health import SEVERITY_RANK

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    key TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    pipeline TEXT NOT NULL,
    dataset_ids TEXT NOT NULL,
    processing_date TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    notified_severity TEXT
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    dataset_ids TEXT NOT NULL,
    worst TEXT NOT NULL,
    critical INTEGER NOT NULL,
    warning INTEGER NOT NULL,
    info INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    def record(self, result):
        """Upsert this check's findings and close the ones that went away. Returns (new, escalated, resolved)."""
        now = result.checked_at.isoformat(timespec="seconds")
        current = {f.key: f for f in result.findings}
        scope = {p.key for p in result.pipelines}
        judged = {(p.key, d.processing_date.isoformat()): d.state for p in result.pipelines for d in p.dates}
        new, escalated, resolved = [], [], []
        for key, item in current.items():
            row = self.db.execute("SELECT * FROM findings WHERE key = ?", (key,)).fetchone()
            day = item.processing_date.isoformat() if item.processing_date else None
            if row is None or row["resolved_at"]:
                self.db.execute(
                    "INSERT OR REPLACE INTO findings (key, kind, severity, title, pipeline, dataset_ids, "
                    "processing_date, first_seen, last_seen, resolved_at, resolution, notified_severity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                    (key, item.kind, item.severity, item.title, item.pipeline, json.dumps(item.dataset_ids), day,
                     now, now))
                new.append(item)
            else:
                if SEVERITY_RANK[item.severity] > SEVERITY_RANK[row["severity"]]:
                    escalated.append(item)
                self.db.execute("UPDATE findings SET severity = ?, title = ?, last_seen = ? WHERE key = ?",
                                (item.severity, item.title, now, key))
        for row in self.db.execute("SELECT * FROM findings WHERE resolved_at IS NULL").fetchall():
            if row["key"] in current or row["pipeline"] not in scope:
                continue
            state = judged.get((row["pipeline"], row["processing_date"])) if row["processing_date"] else None
            if row["processing_date"] and state is None:
                resolution = "left the check window"
            elif state in ("published", "recovered"):
                resolution = f"date {state}"
            else:
                resolution = "no longer reported"
            self.db.execute("UPDATE findings SET resolved_at = ?, resolution = ? WHERE key = ?",
                            (now, resolution, row["key"]))
            resolved.append((dict(row), resolution))
        counts = {s: sum(1 for f in result.findings if f.severity == s) for s in ("critical", "warning", "info")}
        self.db.execute("INSERT INTO checks (checked_at, dataset_ids, worst, critical, warning, info) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (now, json.dumps(result.dataset_ids), result.worst, counts["critical"], counts["warning"],
                         counts["info"]))
        self.db.commit()
        return new, escalated, resolved

    def to_notify(self, findings, min_severity):
        """Findings at or above min_severity never notified at their current severity."""
        out = []
        for item in findings:
            if SEVERITY_RANK[item.severity] < SEVERITY_RANK[min_severity]:
                continue
            row = self.db.execute("SELECT notified_severity FROM findings WHERE key = ?", (item.key,)).fetchone()
            done = row["notified_severity"] if row else None
            if done is None or SEVERITY_RANK[item.severity] > SEVERITY_RANK[done]:
                out.append(item)
        return out

    def mark_notified(self, findings):
        for item in findings:
            self.db.execute("UPDATE findings SET notified_severity = ? WHERE key = ?", (item.severity, item.key))
        self.db.commit()

    def open_findings(self):
        rows = self.db.execute("SELECT * FROM findings WHERE resolved_at IS NULL ORDER BY last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def history(self, limit=20):
        rows = self.db.execute("SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()


def utcnow():
    return datetime.now(timezone.utc)
