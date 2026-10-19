"""Time zones for the job schedules, without depending on the system's tz database.

zoneinfo needs IANA data, which slim containers do not ship (and pip's tzdata may not be installed). The data
jobs only use a handful of zones, so when zoneinfo cannot load one, these fixed rules stand in: US Eastern since
the 2007 DST rules and the UK since 1996, both exact for every date the monitor looks at.
"""
from datetime import datetime, timedelta, timezone, tzinfo

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo, ZoneInfoNotFoundError = None, Exception


def _nth_sunday(year, month, n):
    day = datetime(year, month, 1)
    day += timedelta(days=(6 - day.weekday()) % 7)
    return day + timedelta(weeks=n - 1)


def _last_sunday(year, month):
    day = datetime(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - 6) % 7)


class _RuleZone(tzinfo):
    """A standard offset plus one hour of DST between two UTC instants each year."""

    def __init__(self, name, standard_hours, dst_start_utc, dst_end_utc):
        self._name, self._standard = name, timedelta(hours=standard_hours)
        self._start, self._end = dst_start_utc, dst_end_utc

    def _dst_window(self, year):
        return self._start(year), self._end(year)

    def _is_dst_utc(self, utc_naive):
        start, end = self._dst_window(utc_naive.year)
        return start <= utc_naive < end

    def utcoffset(self, dt):
        # dt is local wall time. Read it both ways; when both are valid (the repeated hour at the end of DST),
        # fold picks the occurrence (PEP 495); when neither is (the skipped hour), the pre-transition offset.
        naive = dt.replace(tzinfo=None)
        summer = self._standard + timedelta(hours=1)
        as_summer = self._is_dst_utc(naive - summer)
        as_standard = not self._is_dst_utc(naive - self._standard)
        if as_summer and as_standard:
            return summer if dt.fold == 0 else self._standard
        if as_summer:
            return summer
        return self._standard

    def dst(self, dt):
        return self.utcoffset(dt) - self._standard

    def tzname(self, dt):
        return self._name

    def fromutc(self, dt):
        naive = dt.replace(tzinfo=None)
        summer = self._is_dst_utc(naive)
        local = naive + self._standard + (timedelta(hours=1) if summer else timedelta(0))
        # The first standard-time hour after DST repeats a wall time: mark it as the second occurrence.
        fold = 1 if not summer and self._is_dst_utc(naive - timedelta(hours=1)) else 0
        return local.replace(tzinfo=self, fold=fold)


# US: second Sunday of March 02:00 local (07:00 UTC) to first Sunday of November 02:00 local (06:00 UTC).
_NEW_YORK = _RuleZone("America/New_York", -5,
                      lambda y: _nth_sunday(y, 3, 2) + timedelta(hours=7),
                      lambda y: _nth_sunday(y, 11, 1) + timedelta(hours=6))
# UK: last Sunday of March 01:00 UTC to last Sunday of October 01:00 UTC.
_LONDON = _RuleZone("Europe/London", 0,
                    lambda y: _last_sunday(y, 3) + timedelta(hours=1),
                    lambda y: _last_sunday(y, 10) + timedelta(hours=1))
_FALLBACK = {"America/New_York": _NEW_YORK, "US/Eastern": _NEW_YORK, "Europe/London": _LONDON,
             "UTC": timezone.utc, "Etc/UTC": timezone.utc}


def get_zone(name, allow_system=True):
    """zoneinfo when the system has the data, else the fixed rules above."""
    name = name or "UTC"
    if allow_system and ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    if name in _FALLBACK:
        return _FALLBACK[name]
    raise ValueError(f"time zone {name!r} is not available (install tzdata, or add a rule to dsmon/tz.py)")
