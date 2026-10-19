from datetime import date

from dsmon.health import Finding
from dsmon.holidays import non_business_reason
from dsmon.inventory import _job_from_record
from dsmon.logs import Diagnosis
from dsmon.runbook import actions_for
from dsmon.runs import parse_run

LOG_URL = ("https://s3.amazonaws.com/datafleet.quantconnect.com/logs/325/2026-10-14/809-fa783f62-2241-4708-a600-"
           "e913e7ab57f3-20261014-140034_20261013_8cd900380b21_us_bureau_of_labor_statistics_bls.log?X=1")


def test_processing_date_comes_from_the_log_name():
    run = parse_run({"id": 1, "jobId": 325, "time": "2026-10-14 14:00:34", "status": "Failed", "logs": LOG_URL})
    assert run.processing_date == date(2026, 10, 13)
    assert run.chain_slug == "us_bureau_of_labor_statistics_bls"


def test_processing_date_falls_back_to_the_offset_while_the_log_is_missing():
    run = parse_run({"id": 1, "jobId": 383, "time": "2026-10-14 17:02:33", "status": "Complete", "logs": None}, offset=0)
    assert run.processing_date == date(2026, 10, 14)


def test_us_holidays():
    assert non_business_reason(date(2026, 10, 12)) == "Columbus Day"
    assert non_business_reason(date(2026, 10, 10)) == "weekend"
    assert non_business_reason(date(2026, 10, 13)) is None
    assert non_business_reason(date(2026, 7, 3)) == "Independence Day"  # July 4th 2026 is a Saturday


def test_job_config_secrets_are_not_kept():
    record = {"jobId": 1, "name": "X", "status": "Enabled", "schedule": "0 1 * * *", "timezone": "UTC", "offset": 1,
              "timeout": "01:30:00", "jobChain": [],
              "configuration": [{"name": "tiingo-api-token", "value": "abc123"},
                                {"name": "qc-datafleet-alert-emails", "value": "[\"a@b.com\"]"}]}
    job = _job_from_record(record)
    assert job.config["tiingo-api-token"] == "*"
    assert job.alert_emails == ["a@b.com"]
    assert job.timeout_minutes == 90


def _failed(offset):
    item = Finding("date_failed", "critical", "t", "p", [88], processing_date=date(2026, 10, 13))
    item.context["offset"] = offset
    item.diagnosis = Diagnosis("code_error", "KeyError: 'close'")
    return item


def test_manual_run_warning_only_for_offset_jobs():
    with_offset = " ".join(actions_for(_failed(1)))
    without = " ".join(actions_for(_failed(0)))
    assert "Manual Run" in with_offset and "Never pick today's date" in with_offset
    assert "Manual Run" in without and "Never pick today's date" not in without
