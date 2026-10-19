"""Expected publication days of a dated directory, by calendar.

A directory's calendar is declared in config.toml ([content.calendars]): "equity-usa" (US equity sessions),
"federal" (US federal business days: EDGAR, FDIC), "none" (event-driven, no expected days), each with an optional
"+N" for datasets stamped N days after the session (Factors writes Tuesday to Saturday). US equity sessions come
from the NYSE holiday rules in holidays.py.
"""
from datetime import timedelta

from .holidays import nyse_holidays, us_federal_holidays

def parse(spec):
    """'equity-usa+1' -> ('equity-usa', 1)."""
    name, _, shift = spec.partition("+")
    return name.strip(), int(shift or 0)


def expected_days(spec, start, end):
    """Days in [start, end] a directory with this calendar should have a file for; None for 'none'."""
    name, shift = parse(spec)
    if name == "none":
        return None
    first, last = start - timedelta(days=shift), end - timedelta(days=shift)
    if name == "equity-usa":
        closed = set().union(*(nyse_holidays(y) for y in range(first.year, last.year + 1)))
        sessions = _weekdays(first, last, closed)
    elif name == "federal":
        closed = set().union(*(us_federal_holidays(y) for y in range(first.year, last.year + 1)))
        sessions = _weekdays(first, last, closed)
    else:
        raise ValueError(f"unknown calendar {spec!r}")
    return [d + timedelta(days=shift) for d in sessions]


def _weekdays(first, last, closed):
    out, day = [], first
    while day <= last:
        if day.weekday() < 5 and day not in closed:
            out.append(day)
        day += timedelta(days=1)
    return out
