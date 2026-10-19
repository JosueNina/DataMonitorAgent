import zipfile

import pytest

from dsmon.sensor import watch_core as core


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def data(tmp_path):
    root = tmp_path / "Data"
    # per-ticker files: one healthy, one out of order with a duplicate date and a malformed row
    write(root / "alternative/vendor/ds/aapl.csv", "20261012,1.0,2\n20261013,1.1,3\n")
    write(root / "alternative/vendor/ds/msft.csv", "20261013,2.0,1\n20261012,2.1,1\n20261012,2.2,1\n20261014,2.3\n")
    # dated universe files, with an identity collision on the newest date
    write(root / "alternative/vendor/ds/universe/20261012.csv", "AAPL R735QTJ8XC9X,AAPL,1\n")
    write(root / "alternative/vendor/ds/universe/20261013.csv",
          "AAPL R735QTJ8XC9X,AAPL,1\nAAPL ABCDEFGHIJKL,AAPL,2\nMSFT R735QTJ8XC9Y,MSFT,3\n")
    # a zipped file
    with zipfile.ZipFile(root / "alternative/vendor/ds/goog.zip", "w") as archive:
        archive.writestr("goog.csv", "20261013,5\n")
    return root


def test_finds_dataset_directories(data):
    dirs, _ = core.find_dataset_dirs(str(data), ["alternative"])
    rel = sorted(d.replace(str(data), "").replace("\\", "/").strip("/") for d in dirs)
    assert rel == ["alternative/vendor/ds", "alternative/vendor/ds/universe"]


def _zip_lines(path):
    with zipfile.ZipFile(path) as archive:
        return archive.read(archive.namelist()[0]).decode().splitlines()


def test_per_entity_files_report_order_duplicates_and_malformed_rows(data):
    facts = core.scan_dataset(str(data / "alternative/vendor/ds"), str(data), zip_reader=_zip_lines)
    files = facts["files"]
    assert files["sampled"] == 3
    assert files["unsorted"] == 1 and files["dups"] == 1 and files["bad"] == 1
    assert files["newest_last"] == "20261014"
    assert files["worst"][0]["f"] == "msft.csv"


def test_zips_without_a_reader_are_skipped_not_errors(data):
    files = core.scan_dataset(str(data / "alternative/vendor/ds"), str(data))["files"]
    assert files["sampled"] == 2 and files["skipped"] == 1 and not files["errors"]


def test_dated_files_report_rows_and_collisions(data):
    facts = core.scan_dataset(str(data / "alternative/vendor/ds/universe"), str(data))
    dated = facts["dated"]
    assert dated["count"] == 2 and dated["newest"] == "20261013"
    assert dated["recent"][-1]["rows"] == 3 and dated["recent"][-1]["collisions"] == 1


def test_large_files_are_read_partially_with_the_true_last_date(data, monkeypatch):
    path = data / "alternative/vendor/big/x.csv"
    write(path, "".join(f"2026{m:02d}01,{m}\n" for m in range(1, 13)))
    monkeypatch.setattr(core, "MAX_BYTES", 30)
    rows = core.read_rows(str(path), max_bytes=30)
    assert getattr(rows, "partial", False)
    facts = core.scan_dataset(str(path.parent), str(data))
    assert facts["files"]["newest_last"] == "20261201"


def test_codec_round_trip_in_any_order():
    payload = {"v": 1, "datasets": [{"p": f"alternative/x/{i}", "n": i} for i in range(500)]}
    chunks = core.encode(payload, chunk_chars=200)
    assert len(chunks) > 1
    assert core.decode(reversed(["noise line"] + chunks)) == payload
    with pytest.raises(ValueError):
        core.decode(chunks[1:])
