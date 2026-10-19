from datetime import date

from dsmon.health import DateState, Finding, PipelineStatus
from dsmon.monitor import CheckResult
from dsmon.store import Store
from tests.conftest import utc


def result(at, findings, date_state="failed"):
    status = PipelineStatus("bls", [88], [325], "failing",
                            dates=[DateState(date(2026, 10, 13), date_state, [])])
    return CheckResult(utc(at), 36, [88], [status], findings)


def failed(severity="warning"):
    return Finding("date_failed", severity, "2026-10-13 was not published", "bls", [88],
                   processing_date=date(2026, 10, 13))


def test_new_then_escalated_then_resolved(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    new, escalated, resolved = store.record(result("2026-10-14T15:00:00", [failed("warning")]))
    assert len(new) == 1 and not escalated and not resolved

    new, escalated, _ = store.record(result("2026-10-14T16:00:00", [failed("critical")]))
    assert not new and len(escalated) == 1

    _, _, resolved = store.record(result("2026-10-15T15:00:00", [], date_state="published"))
    assert resolved and resolved[0][1] == "date published"
    assert store.open_findings() == []


def test_notified_once_per_severity(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    first = failed("warning")
    store.record(result("2026-10-14T15:00:00", [first]))
    assert store.to_notify([first], "warning") == [first]
    store.mark_notified([first])
    assert store.to_notify([first], "warning") == []

    worse = failed("critical")
    store.record(result("2026-10-14T16:00:00", [worse]))
    assert store.to_notify([worse], "warning") == [worse]


def test_info_findings_are_not_notified(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    item = failed("info")
    store.record(result("2026-10-14T15:00:00", [item]))
    assert store.to_notify([item], "warning") == []
