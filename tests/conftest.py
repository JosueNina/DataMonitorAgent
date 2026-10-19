import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dsmon.inventory import Inventory  # noqa: E402
from dsmon.runs import Run, SyncResult  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def inventory():
    return Inventory.from_json(json.loads((FIXTURES / "inventory_subset.json").read_text(encoding="utf-8")))


def fixture_log(name):
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


def utc(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


_ids = iter(range(1, 10 ** 6))


def make_run(job_id, started, status="Complete", processing_date=None, sync=None, offset=1, queue="primary"):
    """A run row. sync: None (no sync), "published", "skipped" or "failed"."""
    started = utc(started)
    rows = {"published": [SyncResult("Live 01", "Complete"), SyncResult("Live 02", "Complete")],
            "skipped": [SyncResult("Live 01", "Skipped"), SyncResult("Live 02", "Skipped")],
            "failed": [SyncResult("Live 01", "Failed")], None: []}[sync]
    day = processing_date or (started.date() - timedelta(days=offset))
    return Run(next(_ids), job_id, 1, queue, started, status, 0 if status == "Complete" else 1, None, None, rows, day)
