import json
import os
import time

from dsmon.retention import prune

DAY = 86400


def touch(path, age_days, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


def test_failed_runs_outlive_complete_ones(tmp_path):
    touch(tmp_path / "logs/325/1.log.gz", 40)
    touch(tmp_path / "logs/325/1.diag.json", 40, json.dumps({"category": "ok"}))
    touch(tmp_path / "logs/325/2.log.gz", 40)
    touch(tmp_path / "logs/325/2.diag.json", 40, json.dumps({"category": "source_bad_content"}))
    touch(tmp_path / "logs/325/3.log.gz", 5)
    removed = prune(tmp_path)
    assert removed["logs"] == 2
    assert sorted(p.name for p in (tmp_path / "logs/325").iterdir()) == ["2.diag.json", "2.log.gz", "3.log.gz"]


def test_snapshots_are_kept_by_count_and_reports_by_age(tmp_path):
    for i in range(35):
        touch(tmp_path / f"content/2026{i:04d}.json", 0)
    touch(tmp_path / "runs/old.md", 100)
    touch(tmp_path / "runs/latest.md", 100)
    removed = prune(tmp_path)
    assert removed["snapshots"] == 5 and len(list((tmp_path / "content").glob("*.json"))) == 30
    assert removed["reports"] == 1 and (tmp_path / "runs/latest.md").exists()


def test_expired_log_markers_are_dropped_after_a_while(tmp_path):
    touch(tmp_path / "logs/97/9.missing", 20)
    assert prune(tmp_path)["markers"] == 1
