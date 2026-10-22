from datetime import date, timedelta

from dsmon.health import Settings, coverage_gaps, evaluate_pipeline
from tests.conftest import make_run, utc

NOW = utc("2026-10-14T18:00:00")
START = NOW - timedelta(hours=36)


def pipeline_for(inventory, dataset_id):
    pipelines = inventory.pipelines([dataset_id])
    assert len(pipelines) == 1, [p.job_ids for p in pipelines]
    return pipelines[0]


def states(status):
    return {d.processing_date.isoformat(): d.state for d in status.dates}


def kinds(status):
    return sorted((f.kind, f.severity) for f in status.findings)


def test_bls_failed_date_without_redundancy_is_critical(inventory):
    runs = {325: [make_run(325, "2026-10-14T14:00:34", "Failed"),
                  make_run(325, "2026-10-13T14:00:33", sync="published")]}
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, runs, NOW, START)
    assert states(status) == {"2026-10-12": "published", "2026-10-13": "failed"}
    assert kinds(status) == [("date_failed", "critical"), ("redundancy_disabled", "warning")]
    assert status.state == "failing"


def test_spectral_failure_covered_by_redundancy_is_recovered(inventory):
    runs = {336: [make_run(336, "2026-10-14T12:07:04", "Failed"), make_run(336, "2026-10-13T12:04:53", sync="published")],
            337: [make_run(337, "2026-10-14T13:15:33", sync="published"), make_run(337, "2026-10-13T13:15:33", sync="skipped")]}
    status = evaluate_pipeline(pipeline_for(inventory, 123), inventory, runs, NOW, START)
    assert states(status) == {"2026-10-12": "published", "2026-10-13": "recovered"}
    assert kinds(status) == [("date_recovered", "info")]
    assert status.state == "ok"


def test_failure_with_redundancy_still_due_is_pending(inventory):
    now = utc("2026-10-14T12:30:00")
    runs = {336: [make_run(336, "2026-10-14T12:07:04", "Failed"), make_run(336, "2026-10-13T12:04:53", sync="published")],
            337: [make_run(337, "2026-10-13T13:15:33", sync="skipped")]}
    status = evaluate_pipeline(pipeline_for(inventory, 123), inventory, runs, now, now - timedelta(hours=36))
    assert states(status)["2026-10-13"] == "failed_pending"
    assert ("date_failed_pending", "warning") in kinds(status)


def test_scheduled_run_that_never_started(inventory):
    runs = {325: [make_run(325, "2026-10-13T14:00:33", sync="published")]}
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, runs, NOW, START)
    assert states(status)["2026-10-13"] == "not_run"
    assert ("date_not_run", "critical") in kinds(status)


def test_run_in_progress_for_hours_is_stuck(inventory):
    runs = {325: [make_run(325, "2026-10-14T14:00:34", "In Progress"),
                  make_run(325, "2026-10-13T14:00:33", sync="published")]}
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, runs, utc("2026-10-15T02:00:00"),
                               START, Settings(stuck_hours=6))
    assert states(status)["2026-10-13"] == "stuck"


def test_completed_chain_step_does_not_hide_a_failed_step(inventory):
    # SEC Filings 2026-10-12: the redundancy's "sleep 1s" step completed without a sync, its real step failed.
    runs = {13: [make_run(13, "2026-10-13T17:00:33", "Failed"), make_run(13, "2026-10-14T17:00:33", sync="published")],
            30: [make_run(30, "2026-10-13T19:00:34", "Failed", queue="redundancy"),
                 make_run(30, "2026-10-13T19:00:33", queue="redundancy")]}
    status = evaluate_pipeline(pipeline_for(inventory, 9), inventory, runs, utc("2026-10-14T20:00:00"), START)
    assert states(status)["2026-10-12"] == "failed"


def test_redundancies_pair_with_their_primary_despite_different_names(inventory):
    assert pipeline_for(inventory, 9).job_ids == [13, 30]       # "Alternative U.S. Equity SEC" / "Redundancy Alternative Equity SEC"
    assert sorted(pipeline_for(inventory, 33).job_ids) == [87, 179]  # "Vendor Brain..." / "Redundancy Alternative Brain..."
    options = {tuple(sorted(p.job_ids)) for p in inventory.pipelines([40])}
    assert options == {(4, 26), (367, 368)}


def test_coverage_detects_a_redundancy_on_another_date(inventory):
    jobs = inventory.jobs
    now = utc("2026-10-14T00:00:00")
    # Treasury yield curve: 19:00 NY offset 0 = date D, 21:00 NY offset 0 runs after midnight UTC = date D+1.
    total, gaps = coverage_gaps([jobs[19]], [jobs[36]], now=now)
    assert total and len(gaps) == total
    # Fear and Greed: the redundancy uses offset 2.
    assert coverage_gaps([jobs[261]], [jobs[262]], now=now)


def test_coverage_accepts_backups_that_cross_midnight(inventory):
    jobs = inventory.jobs
    now = utc("2026-10-14T00:00:00")
    assert coverage_gaps([jobs[380]], [jobs[381]], now=now) is None  # 13F: 23:00 and 01:00 NY, both offset 1
    assert coverage_gaps([jobs[367]], [jobs[368]], now=now) is None  # 18:00 offset 0 and 22:00 offset 1


def test_a_redundancy_at_the_same_minute_covers_the_date():
    # Brain Wikipedia Page Views: both jobs at 12:00 UTC Mon-Fri, offset 0.
    from dsmon.inventory import Job
    primary = Job(294, "Brain Wikipedia", True, "0 12 * * 1-5", "UTC", 0, "primary", "", 0, [], [128])
    backup = Job(295, "Redundancy Brain Wikipedia", True, "0 12 * * 1-5", "UTC", 0, "redundancy", "", 0, [], [128])
    assert coverage_gaps([primary], [backup], now=utc("2026-10-19T00:00:00")) is None


def test_a_stuck_manual_run_on_a_date_no_cron_processes_is_not_re_run():
    from dsmon.inventory import Job
    from dsmon.health import scheduled_date
    from dsmon.runbook import actions_for
    from dsmon.health import Finding
    from datetime import date
    job = Job(211, "Security Master", True, "0 4 * * 2-6", "America/New_York", 1, "primary", "", 0, [], [4])
    assert scheduled_date([job], date(2026, 10, 16))       # Friday: the Saturday 04:00 run
    assert not scheduled_date([job], date(2026, 10, 18))   # Sunday: nothing fires on Monday
    item = Finding("run_stuck", "critical", "stuck", "sm", [4], [211], date(2026, 10, 18), [1090444])
    item.context["off_schedule_date"] = True
    assert all("Manual Run" not in a or "default date" in a for a in actions_for(item))
