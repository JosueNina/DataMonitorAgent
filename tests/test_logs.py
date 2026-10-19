import gzip

import pytest

from dsmon.logs import LogArchive, diagnose, sanitize_url, signal_lines
from tests.conftest import fixture_log, make_run

# Real DataFleet logs from 2026-10-08..14 and the category each one must get.
CASES = [
    ("325-1087442.log", "source_bad_content"),    # BLS: 200 with a body of null bytes, retries exhausted
    ("97-1087295.log", "source_tls"),             # ETF constituents: FTP certificate does not match the host
    ("110-1086213.log", "source_not_published"),  # ExtractAlpha: FTP 550, file not there yet
    ("13-1086898.log", "source_not_published"),   # SEC: day missing from the feed index (Columbus Day)
    ("346-1085822.log", "source_unavailable"),    # BEA: 503
    ("345-1085782.log", "source_unavailable"),    # BEA: HttpClient timeout
    ("336-1087348.log", "missing_input"),         # Spectral: SPY constituents file missing
    ("275-1087233.log", "missing_input"),         # Aggregation: input directory missing
    ("286-1083892.log", "missing_input"),         # OANDA export: found no symbols
]


@pytest.mark.parametrize("name,category", CASES)
def test_failed_runs_get_their_cause(name, category):
    diagnosis = diagnose(fixture_log(name), exit_code=1, status="Failed")
    assert diagnosis.category == category
    assert diagnosis.summary


def test_complete_run_is_ok():
    diagnosis = diagnose(fixture_log("337-1087401.log"), exit_code=0, status="Complete")
    assert diagnosis.category == "ok"
    assert diagnosis.duration_s == pytest.approx(19, abs=2)


def test_root_error_prefers_the_python_exception():
    diagnosis = diagnose(fixture_log("325-1087442.log"), exit_code=1, status="Failed")
    assert diagnosis.summary == "RuntimeError: No release schedule was scraped for [2025]"
    assert any("null bytes" in line for line in diagnosis.evidence)


def test_http_status_is_extracted():
    assert diagnose(fixture_log("346-1085822.log"), exit_code=1, status="Failed").http_status == 503


def test_startup_noise_is_dropped():
    messages = [m for _, _, m in signal_lines(fixture_log("13-1086898.log"))]
    assert not any("Composer.LoadPartsSafely" in m for m in messages)


def test_unrecognized_failure_is_a_code_error():
    text = ("2026-10-14T10:00:00.0000000Z TRACE:: start\n"
            "2026-10-14T10:00:01.0000000Z ERROR:: KeyError: 'close'\n")
    assert diagnose(text, exit_code=1, status="Failed").category == "code_error"


def test_sanitize_url_drops_query_with_keys():
    assert sanitize_url("https://api.example.gov/data?api_key=SECRET&x=1") == "https://api.example.gov/data"


def test_archive_keeps_logs_after_the_url_expires(tmp_path):
    archive = LogArchive(tmp_path)
    run = make_run(325, "2026-10-14T14:00:34", status="Failed")
    path = archive.path(run)
    path.parent.mkdir(parents=True)
    path.write_bytes(gzip.compress(b"archived text"))
    run.log_url = "https://expired.invalid/log"
    assert archive.fetch(run) == "archived text"
