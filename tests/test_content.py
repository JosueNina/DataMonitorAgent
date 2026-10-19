from datetime import date, datetime, timedelta, timezone

from dsmon.content import judge

TODAY = date(2026, 10, 15)


def business_days(start, end, skip=()):
    days, day = [], start
    while day <= end:
        if day.weekday() < 5 and day not in skip and day != date(2026, 10, 12):  # Columbus Day
            days.append(day)
        day += timedelta(days=1)
    return days


def dated_facts(days, rows=500, overrides=None):
    overrides = overrides or {}
    recent = [{"d": d.strftime("%Y%m%d"), "rows": overrides.get(d, rows)} for d in days]
    return {"count": len(days), "newest": recent[-1]["d"], "recent": recent}


def snapshot(datasets, at=None):
    at = at or datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
    return {"v": 1, "at": int(at.timestamp()), "datasets": datasets}


def kinds(findings):
    return sorted(f.kind for f in findings)


def test_a_missing_business_day_is_found_and_holidays_are_not():
    days = business_days(date(2026, 9, 20), date(2026, 10, 14), skip={date(2026, 10, 7)})
    current = snapshot([{"p": "alternative/fdic/banks/universe", "dated": dated_facts(days)}])
    found = judge(current, [], {151: ["alternative/fdic"]}, today=TODAY)
    assert kinds(found) == ["content_missing_dates"]
    assert found[0].processing_date == date(2026, 10, 7) and found[0].dataset_ids == [151]
    assert "2026-10-12" not in found[0].detail[0]


def test_a_thin_date_is_found():
    days = business_days(date(2026, 9, 20), date(2026, 10, 14))
    current = snapshot([{"p": "alternative/x/universe", "dated": dated_facts(days, overrides={days[-1]: 40})}])
    assert kinds(judge(current, [], today=TODAY)) == ["content_thin_date"]


def test_identity_collisions_and_unsorted_rows():
    days = business_days(date(2026, 9, 20), date(2026, 10, 14))
    dated = dated_facts(days)
    dated["recent"][-1].update(collisions=2, collision_examples=["ADCT"])
    files = {"sampled": 40, "rows": 1000, "unsorted": 3, "dups": 0, "bad": 0, "errors": [],
             "worst": [{"f": "a.csv", "unsorted": 3, "dups": 0, "bad": 0, "rows": 10, "last": "20261014"}]}
    current = snapshot([{"p": "alternative/x/universe", "dated": dated}, {"p": "alternative/x", "files": files}])
    assert kinds(judge(current, [], today=TODAY)) == ["content_identity_collision", "content_unsorted"]


def test_duplicates_only_alarm_when_they_grow():
    def files(dups):
        return {"sampled": 40, "rows": 1000, "unsorted": 0, "dups": dups, "bad": 0, "errors": [], "worst": []}
    before = snapshot([{"p": "alternative/fdic/financials", "files": files(14)}])
    same = snapshot([{"p": "alternative/fdic/financials", "files": files(14)}])
    grew = snapshot([{"p": "alternative/fdic/financials", "files": files(80)}])
    assert judge(same, [before], today=TODAY) == []
    assert kinds(judge(grew, [before], today=TODAY)) == ["content_dups_grew"]


def test_newest_date_that_stopped_moving_is_stale():
    files = {"sampled": 5, "rows": 50, "unsorted": 0, "dups": 0, "bad": 0, "errors": [], "worst": [],
             "newest_last": "20260706"}
    old = [snapshot([{"p": "alternative/finra/monthlysummary", "files": files}],
                    at=datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(days=i)) for i in range(3)]
    current = snapshot([{"p": "alternative/finra/monthlysummary", "files": files}])
    assert "content_stale" in kinds(judge(current, old, today=TODAY))


def test_caches_are_ignored():
    files = {"sampled": 1, "rows": 9, "unsorted": 5, "dups": 0, "bad": 0, "errors": [], "worst": []}
    assert judge(snapshot([{"p": "alternative/uspto/cache", "files": files}]), [], today=TODAY) == []


def test_weekdays_are_read_from_the_data_and_one_odd_saturday_is_ignored():
    days = business_days(date(2026, 9, 20), date(2026, 10, 14)) + [date(2026, 10, 10)]
    current = snapshot([{"p": "alternative/finra/shortsalevolume/universe", "dated": dated_facts(sorted(days))}])
    assert judge(current, [], today=TODAY) == []


def test_datasets_stamped_the_day_after_the_session_tuesday_to_saturday():
    days, day = [], date(2026, 9, 22)
    while day <= date(2026, 10, 14):
        if day.weekday() in (1, 2, 3, 4, 5):
            days.append(day)
        day += timedelta(days=1)
    current = snapshot([{"p": "alternative/quantconnect/factors/equity/universe", "dated": dated_facts(days)}])
    assert judge(current, [], today=TODAY) == []


def test_columbus_day_is_a_trading_day_for_market_calendars():
    from dsmon.holidays import market_closed_reason
    assert market_closed_reason(date(2026, 10, 12)) is None
    assert market_closed_reason(date(2026, 4, 3)) == "Good Friday"


def test_declared_calendar_decides_the_expected_days():
    from dsmon.calendars import expected_days
    days = business_days(date(2026, 9, 20), date(2026, 10, 14), skip={date(2026, 10, 7)}) + [date(2026, 10, 12)]
    current = snapshot([{"p": "alternative/finra/shortsalevolume/universe", "dated": dated_facts(sorted(days))}])
    found = judge(current, [], today=TODAY, calendars={"alternative/finra": "equity-usa"})
    assert kinds(found) == ["content_missing_dates"] and found[0].processing_date == date(2026, 10, 7)
    # Factors: stamped the day after the session, Tuesday to Saturday
    shifted = expected_days("equity-usa+1", date(2026, 10, 5), date(2026, 10, 10))
    assert [d.weekday() for d in shifted] == [1, 2, 3, 4, 5]
    assert expected_days("none", date(2026, 10, 5), date(2026, 10, 10)) is None
    # Good Friday is closed for equities, open for the federal calendar
    assert date(2026, 4, 3) not in expected_days("equity-usa", date(2026, 4, 1), date(2026, 4, 6))
    assert date(2026, 4, 3) in expected_days("federal", date(2026, 4, 1), date(2026, 4, 6))
