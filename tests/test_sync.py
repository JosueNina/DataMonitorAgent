from datetime import timedelta

from dsmon.health import evaluate_pipeline
from dsmon.logs import SyncInfo, parse_sync_log
from tests.conftest import fixture_log, make_run, utc
from tests.test_health import NOW, START, pipeline_for


def test_first_sync_records_when_data_reached_the_node():
    info = parse_sync_log(fixture_log("sync-first.log"))
    assert info.decision == "first"
    assert info.synced_at is not None and info.synced_at >= info.started_at
    assert info.tar and info.tar.endswith(".tar")


def test_skipped_sync_points_at_the_prior_sync():
    info = parse_sync_log(fixture_log("sync-skipped.log"))
    assert info.decision == "skipped"
    assert info.prior_sync_at is not None and info.synced_at is None


def test_new_cron_turn_publishes_again():
    assert parse_sync_log(fixture_log("sync-again.log")).decision == "again"


def test_manual_run_with_synchronize_always_is_forced():
    assert parse_sync_log(fixture_log("sync-forced.log")).decision == "forced"


def _with_info(run, decision, synced_at=None, prior=None):
    for sync in run.sync:
        sync.info = SyncInfo(decision, synced_at=utc(synced_at) if synced_at else None,
                             prior_sync_at=utc(prior) if prior else None)
    return run


def test_publication_time_and_lag(inventory):
    run = _with_info(make_run(325, "2026-10-14T14:00:34", sync="published"), "first", "2026-10-14T14:04:34")
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: [run]}, NOW, START)
    day = next(d for d in status.dates if d.state == "published")
    assert day.published_at == utc("2026-10-14T14:04:34")
    assert day.publish_lag_minutes == 4.0


def test_failed_node_without_cover_is_a_warning(inventory):
    run = make_run(325, "2026-10-14T14:00:34", sync="published")
    run.sync[1].status = "Failed"
    status = evaluate_pipeline(pipeline_for(inventory, 88), inventory, {325: [run]}, NOW, START)
    assert any(f.kind == "sync_failed" and f.severity == "warning" for f in status.findings)


def test_every_date_published_twice_is_one_finding(inventory):
    runs = {19: [], 36: []}
    for day in ("2026-10-12", "2026-10-13"):
        runs[19].append(_with_info(make_run(19, f"{day}T23:00:33", sync="published", offset=0), "first", f"{day}T23:01:30"))
        runs[36].append(_with_info(make_run(36, f"{day}T23:30:33", sync="published", offset=0, queue="redundancy"),
                                   "again", f"{day}T23:31:30"))
    status = evaluate_pipeline(pipeline_for(inventory, 12), inventory, runs, NOW, NOW - timedelta(hours=72))
    again = [f for f in status.findings if f.kind == "published_again"]
    assert len(again) == 1  # one per pipeline, not one per date
    assert sum(1 for line in again[0].detail if line.startswith("2026-10-1")) == 2
