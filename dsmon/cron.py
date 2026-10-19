"""Minimal 5-field cron evaluator, enough for the DataFleet schedules (lists, ranges, steps, names)."""
from datetime import datetime, timedelta, timezone
from .tz import get_zone

_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MONTH_NAMES = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


class CronError(ValueError):
    pass


def _parse_field(text, low, high, names=None):
    values = set()
    for part in text.lower().split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise CronError(f"bad step in {text!r}")
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = _value(start_text, names), _value(end_text, names)
        else:
            start = _value(part, names)
            end = high if step != 1 else start
        if start < low or end > high or start > end:
            raise CronError(f"{text!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return values


def _value(text, names):
    if names and text in names:
        return names[text]
    try:
        return int(text)
    except ValueError as error:
        raise CronError(f"bad cron value {text!r}") from error


class Cron:
    def __init__(self, expression, tz="UTC"):
        fields = expression.split()
        if len(fields) != 5:
            raise CronError(f"expected 5 fields: {expression!r}")
        self.expression = expression
        self.tz = get_zone(tz or "UTC")
        self.minutes = _parse_field(fields[0], 0, 59)
        self.hours = _parse_field(fields[1], 0, 23)
        self.days = _parse_field(fields[2], 1, 31)
        self.months = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
        dows = _parse_field(fields[4], 0, 7, _DOW_NAMES)
        self.dows = {0 if d == 7 else d for d in dows}
        # Standard cron: when both day fields are restricted, a day matches either one.
        self._dom_any = fields[2] == "*"
        self._dow_any = fields[4] == "*"

    def matches_day(self, day):
        if day.month not in self.months:
            return False
        dom = day.day in self.days
        dow = (day.isoweekday() % 7) in self.dows
        if self._dom_any and self._dow_any:
            return True
        if self._dom_any:
            return dow
        if self._dow_any:
            return dom
        return dom or dow

    def fires_between(self, start_utc, end_utc):
        """Fire times (aware, UTC) in [start_utc, end_utc)."""
        start_local = start_utc.astimezone(self.tz)
        end_local = end_utc.astimezone(self.tz)
        day = start_local.date() - timedelta(days=1)
        fires = []
        while day <= end_local.date():
            if self.matches_day(day):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.tz)
                        fire = local.astimezone(timezone.utc)
                        # A wall time skipped by a DST jump does not round-trip; cron does not fire it.
                        if fire.astimezone(self.tz).replace(tzinfo=None) != local.replace(tzinfo=None):
                            continue
                        if start_utc <= fire < end_utc:
                            fires.append(fire)
            day += timedelta(days=1)
        return sorted(set(fires))
