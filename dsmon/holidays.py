"""US federal holidays, to tell a source that did not publish on a holiday from a real outage."""
from datetime import date, timedelta


def _nth_weekday(year, month, weekday, n):
    day = date(year, month, 1)
    day += timedelta(days=(weekday - day.weekday()) % 7)
    return day + timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    day = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _observed(day):
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_federal_holidays(year):
    return {
        _observed(date(year, 1, 1)): "New Year's Day",
        _nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        _nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        _last_weekday(year, 5, 0): "Memorial Day",
        _observed(date(year, 6, 19)): "Juneteenth",
        _observed(date(year, 7, 4)): "Independence Day",
        _nth_weekday(year, 9, 0, 1): "Labor Day",
        _nth_weekday(year, 10, 0, 2): "Columbus Day",
        _observed(date(year, 11, 11)): "Veterans Day",
        _nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        _observed(date(year, 12, 25)): "Christmas Day",
    }


def _easter(year):
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year):
    """NYSE full closures: the federal list without Columbus and Veterans Day, plus Good Friday."""
    days = {d: n for d, n in us_federal_holidays(year).items() if n not in ("Columbus Day", "Veterans Day")}
    days[_easter(year) - timedelta(days=2)] = "Good Friday"
    return days


def market_closed_reason(day):
    """'weekend', an NYSE holiday name, or None for a trading day."""
    if day.weekday() >= 5:
        return "weekend"
    return nyse_holidays(day.year).get(day)


def non_business_reason(day):
    """'weekend', a holiday name, or None for a regular US business day."""
    if day.weekday() >= 5:
        return "weekend"
    return us_federal_holidays(day.year).get(day)
