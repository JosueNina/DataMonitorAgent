from datetime import timedelta

import pytest

from dsmon.cron import Cron, CronError
from tests.conftest import utc


def fires(expression, tz, start, days=7):
    begin = utc(start)
    return Cron(expression, tz).fires_between(begin, begin + timedelta(days=days))


def test_weekdays_in_new_york_convert_to_utc_with_dst():
    # EDT (UTC-4): Tuesday 2026-10-13 20:30 NY is Wednesday 00:30 UTC; Wednesday's own run falls after the window.
    result = fires("30 20 * * 1-5", "America/New_York", "2026-10-14T00:00:00", days=1)
    assert result == [utc("2026-10-14T00:30:00")]


def test_standard_time_after_the_november_switch():
    result = fires("0 19 * * *", "America/New_York", "2026-11-02T12:00:00", days=1)
    assert result == [utc("2026-11-03T00:00:00")]  # EST: 19:00 NY = 00:00 UTC next day


def test_day_names_and_sunday_as_seven():
    names = fires("0 0 * * Tue,Wed,Thu,Fri,Sat", "UTC", "2026-10-11T00:00:00")
    numbers = fires("0 0 * * 2-6", "UTC", "2026-10-11T00:00:00")
    assert names == numbers and len(names) == 5
    assert fires("0 0 * * 7", "UTC", "2026-10-11T00:00:00") == fires("0 0 * * 0", "UTC", "2026-10-11T00:00:00")


def test_steps_and_lists():
    result = fires("*/30 9,15 * * *", "UTC", "2026-10-14T00:00:00", days=1)
    assert [f.strftime("%H:%M") for f in result] == ["09:00", "09:30", "15:00", "15:30"]


def test_restricted_day_of_month_and_week_match_either():
    # Standard cron: "1st of the month OR Monday".
    result = fires("0 0 1 * 1", "UTC", "2026-10-01T00:00:00", days=10)
    assert [f.day for f in result] == [1, 5]


def test_bad_expressions_raise():
    with pytest.raises(CronError):
        Cron("0 25 * * *")
    with pytest.raises(CronError):
        Cron("0 0 * *")


def test_fallback_zones_match_zoneinfo_through_dst_changes():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from dsmon.tz import get_zone
    for name in ("America/New_York", "Europe/London"):
        real, rule = ZoneInfo(name), get_zone(name, allow_system=False)
        hour = datetime(2025, 1, 1, tzinfo=timezone.utc)
        while hour.year < 2028:
            assert hour.astimezone(rule).utcoffset() == hour.astimezone(real).utcoffset(), (name, hour)
            hour += timedelta(hours=1)
        for local in (datetime(2026, 3, 8, 1, 30), datetime(2026, 3, 9, 19, 0), datetime(2026, 11, 2, 19, 0),
                      datetime(2026, 7, 1, 23, 0)):
            assert local.replace(tzinfo=rule).utcoffset() == local.replace(tzinfo=real).utcoffset(), (name, local)
