from datetime import timedelta

from dsmon.health import Settings, evaluate_pipeline
from dsmon.logs import Diagnosis
from dsmon.monitor import Monitor
from tests.conftest import make_run, utc
from tests.test_health import pipeline_for

NOW = utc("2026-10-16T18:00:00")


class _Monitor(Monitor):
    """A monitor whose inventory and diagnoses come from the test, not the API."""

    def __init__(self, inventory, causes, self_healing=(88,)):
        self._inventory, self._causes, self.self_healing = inventory, causes, set(self_healing)

    def inventory(self, refresh=False, max_age_hours=12):
        return self._inventory

    def run_diagnosis(self, run):
        return Diagnosis(self._causes.get(run.id, "unknown"), "")


def test_bls_failing_every_other_day_is_one_recurring_finding(inventory):
    runs = [make_run(325, "2026-10-16T14:01:00", "Failed"),
            make_run(325, "2026-10-15T14:01:00", sync="published"),
            make_run(325, "2026-10-14T14:00:00", "Failed"),
            make_run(325, "2026-10-13T14:00:00", sync="published")]
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: runs}, NOW, NOW - timedelta(hours=36),
                               Settings())
    monitor = _Monitor(inventory, {runs[0].id: "source_bad_content", runs[2].id: "source_bad_content"})
    item = monitor._recurring_failure(status, {325: runs}, NOW)
    # BLS is self-healing: the 10-14 run refilled 10-13, so only one date is lost -> warning, still reported
    assert item.severity == "warning"
    assert item.title.startswith("Failing repeatedly: 2 of the last 4 runs failed, same cause")
    assert "unusable body" in item.title


def test_one_failure_is_not_recurring(inventory):
    runs = [make_run(325, "2026-10-16T14:01:00", "Failed"), make_run(325, "2026-10-15T14:01:00", sync="published")]
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: runs}, NOW, NOW - timedelta(hours=36),
                               Settings())
    assert _Monitor(inventory, {})._recurring_failure(status, {325: runs}, NOW) is None


def test_a_date_the_redundancy_published_still_counts_as_a_failed_attempt(inventory):
    runs336 = [make_run(336, f"2026-10-{d}T12:07:00", "Failed") for d in (14, 15, 16)]
    runs337 = [make_run(337, f"2026-10-{d}T13:15:00", sync="published", queue="redundancy") for d in (14, 15, 16)]
    by_job = {336: runs336, 337: runs337}
    status = evaluate_pipeline(pipeline_for(inventory, 123), inventory, by_job, NOW, NOW - timedelta(hours=36),
                               Settings())
    item = _Monitor(inventory, {r.id: "missing_input" for r in runs336})._recurring_failure(status, by_job, NOW)
    assert item and "3 of the last 6 runs failed" in item.title and item.severity == "warning"
    assert all("another job published the date" in line for line in item.detail[:3])


def test_two_lost_dates_without_self_healing_are_critical(inventory):
    runs = [make_run(325, "2026-10-16T14:01:00", "Failed"), make_run(325, "2026-10-15T14:01:00", sync="published"),
            make_run(325, "2026-10-14T14:00:00", "Failed")]
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: runs}, NOW, NOW - timedelta(hours=36),
                               Settings())
    item = _Monitor(inventory, {}, self_healing=())._recurring_failure(status, {325: runs}, NOW)
    assert item.severity == "critical"


def test_failures_that_lost_nothing_and_recovered_are_info(inventory):
    # BLS 10-24 and 10-25: two weekend failures (the API answered 503), then three good days in a row.
    runs = [make_run(325, "2026-10-16T14:00:00", sync="published"),
            make_run(325, "2026-10-15T14:00:00", sync="published"),
            make_run(325, "2026-10-14T14:00:00", sync="published"),
            make_run(325, "2026-10-13T14:00:00", "Failed"),
            make_run(325, "2026-10-12T14:00:00", "Failed"),
            make_run(325, "2026-10-11T14:00:00", sync="published")]
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: runs}, NOW, NOW - timedelta(hours=36),
                               Settings())
    monitor = _Monitor(inventory, {runs[3].id: "source_unavailable", runs[4].id: "source_unavailable"})
    item = monitor._recurring_failure(status, {325: runs}, NOW)
    assert item is not None and item.severity == "info"
