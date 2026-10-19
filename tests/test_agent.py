import json
import os
import time
from datetime import date
from types import SimpleNamespace

from tests.conftest import utc
from dsmon.cases import prune_cases, write_cases
from dsmon.health import Finding
from dsmon.inventory import Dataset, Inventory, Job
from dsmon.mapping import auto_paths
from dsmon.sensor import watch_core as core


def _job(job_id, repo, dataset_ids):
    return Job(job_id, f"Job {job_id}", True, "0 10 * * *", "UTC", 1, "primary",
               f"https://github.com/QuantConnect/Lean.DataSource.{repo}/commit/abcdef1", 0, [], dataset_ids)


def _inventory():
    datasets = {
        33: Dataset(33, "Brain Sentiment Indicator", "Brain", "brain-sentiment", [1]),
        34: Dataset(34, "Brain ML Stock Ranking", "Brain", "brain-ranking", [2]),
        13: Dataset(13, "VIX Central Contango", "VIX Central", "vix-central", [3]),
        78: Dataset(78, "CNBC Trading", "Quiver Quantitative", "quiver-cnbc", [4]),
        81: Dataset(81, "Insider Trading", "Quiver Quantitative", "quiver-insider", [5]),
        140: Dataset(140, "Fama French", "QuantConnect", "fama-french", [6]),
    }
    jobs = {1: _job(1, "BrainSentiment", [33]), 2: _job(2, "BrainSentiment", [34]), 3: _job(3, "VIXCentral", [13]),
            4: _job(4, "QuiverCNBC", [78]), 5: _job(5, "QuiverQuant", [81]), 6: _job(6, "FamaFrench", [140])}
    return Inventory("2026-10-19T00:00:00+00:00", datasets, jobs, {})


def test_published_folders_map_to_datasets_from_the_inventory():
    paths = ["alternative/brain/sentiment", "alternative/brain/rankings", "alternative/vixcentral",
             "alternative/quiver/cnbc", "alternative/quiver/insidertrading", "alternative/quiver/twitter",
             "alternative/quantconnect/famafrench", "equity/usa/universes/etf/spy"]
    mapping = auto_paths(paths, _inventory(), {"140": ["alternative/quantconnect/famafrench"]})
    assert mapping[33] == ["alternative/brain/sentiment"]
    assert mapping[34] == ["alternative/brain/rankings"]
    assert mapping[13] == ["alternative/vixcentral"]
    assert mapping[78] == ["alternative/quiver/cnbc"]
    assert mapping[81] == ["alternative/quiver/insidertrading"]
    assert mapping[140] == ["alternative/quantconnect/famafrench"]
    # a vendor folder no dataset name matches stays unmapped instead of being pinned on every Quiver dataset
    assert not any("alternative/quiver/twitter" in v for v in mapping.values())


def test_canary_tickers_are_profiled_even_when_older_files_fill_the_sample(tmp_path):
    folder = tmp_path / "Data/alternative/vendor/monthly"
    folder.mkdir(parents=True)
    canary = folder / "aapl.csv"
    canary.write_text("20260701,1\n20260801,2\n", encoding="utf-8")
    old = time.time() - 86400
    os.utime(canary, (old, old))
    for name in ("zzz", "yyy", "xxx"):
        (folder / f"{name}.csv").write_text("20260701,1\n", encoding="utf-8")
    facts = core.scan_dataset(str(folder), str(tmp_path / "Data"), sample_files=2)
    assert facts["files"]["newest_last"] == "20260801"
    assert facts["files"]["sampled"] == 3


def _check(when):
    return SimpleNamespace(checked_at=utc(when))


def _bls_failure():
    return Finding("recurring_failure", "critical", "Failing repeatedly", "bls", [33], [1], None, [11],
                   ["2026-10-14 job 1 run 11: source_bad_content"])


def test_a_case_is_new_once_and_refreshed_by_later_runs(tmp_path):
    inventory = _inventory()
    sections = [("Brain Sentiment Indicator", (33,), "critical", [_bls_failure()], "## Brain: CRITICAL\n"),
                ("VIX Central Contango", (13,), "ok", [], "## VIX: OK\n")]
    first = write_cases(tmp_path, _check("2026-10-19T10:00:00"), sections, inventory)
    assert [p.name for p in first] == ["brain-sentiment-indicator"]
    case = json.loads((first[0] / "case.json").read_text(encoding="utf-8"))
    assert case["status"] == "open" and case["repos"] == ["QuantConnect/Lean.DataSource.BrainSentiment"]
    assert case["new"] == ["recurring_failure|bls|-"]
    assert "Lean.DataSource.BrainSentiment" in (first[0] / "case.md").read_text(encoding="utf-8")

    again = write_cases(tmp_path, _check("2026-10-19T11:00:00"), sections, inventory)
    assert again == []
    case = json.loads((first[0] / "case.json").read_text(encoding="utf-8"))
    assert case["new"] == [] and case["created"].startswith("2026-10-19T10:00")


def test_retention_keeps_open_cases_and_drops_old_closed_ones(tmp_path):
    inventory = _inventory()
    sections = [("Brain Sentiment Indicator", (33,), "critical", [_bls_failure()], "## Brain: CRITICAL\n")]
    write_cases(tmp_path, _check("2026-06-01T10:00:00"), sections, inventory)
    closed = tmp_path / "2026-06-01" / "brain-sentiment-indicator"
    other = Finding("date_failed", "warning", "Date failed", "vix", [13], [3], date(2026, 6, 1))
    write_cases(tmp_path, _check("2026-06-01T10:00:00"),
                [("VIX Central Contango", (13,), "warning", [other], "## VIX\n")], inventory)
    still_open = tmp_path / "2026-06-01" / "vix-central-contango"

    case = json.loads((closed / "case.json").read_text(encoding="utf-8"))
    case["status"] = "closed"
    (closed / "case.json").write_text(json.dumps(case), encoding="utf-8")
    later = time.time() + 100 * 86400
    assert prune_cases(tmp_path, days=90, now=later) == 1
    assert not closed.exists() and still_open.exists()
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert list(index) == ["date_failed|vix|2026-06-01"]


def test_the_run_report_hands_each_dataset_section_to_its_case(tmp_path):
    from dsmon.health import DateState, PipelineStatus
    from dsmon.monitor import CheckResult
    from dsmon.report import run_markdown
    inventory = _inventory()
    failing = PipelineStatus("brain sentiment", [33], [1], "failing", dates=[DateState(date(2026, 10, 18), "failed", [])])
    healthy = PipelineStatus("vix central", [13], [3], "ok", dates=[])
    check = CheckResult(utc("2026-10-19T10:00:00"), 36, [33, 13], [failing, healthy], [_bls_failure()])
    sections = []
    markdown = run_markdown(check, None, inventory, sections_out=sections)
    assert [(s[0], s[2]) for s in sections] == [("Brain Sentiment Indicator", "critical"), ("VIX Central Contango", "ok")]
    assert sections[0][4].startswith("## Brain Sentiment Indicator: CRITICAL") and sections[0][4] in markdown
    cases = write_cases(tmp_path, check, sections, inventory)
    assert [c.name for c in cases] == ["brain-sentiment-indicator"]
