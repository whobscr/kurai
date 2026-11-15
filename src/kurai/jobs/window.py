"""The night window: when model and analysis work may run (system settings, «06:00»–«10:00» by default; it may cross
midnight, e.g. 23:00–05:00)."""

from datetime import datetime, time, timedelta


def parse(hhmm: str) -> time:
    hours, minutes = hhmm.strip().split(":")
    return time(int(hours), int(minutes))


def current(now: datetime, start: str, end: str) -> tuple[datetime, datetime] | None:
    """The window `now` is in, as (start, end) datetimes, or None outside it."""
    s, e = parse(start), parse(end)
    today = now.date()
    if s == e:
        return None
    if s < e:
        begin, finish = datetime.combine(today, s, now.tzinfo), datetime.combine(today, e, now.tzinfo)
        return (begin, finish) if begin <= now < finish else None
    # across midnight
    if now.time() >= s:
        return datetime.combine(today, s, now.tzinfo), datetime.combine(today + timedelta(days=1), e, now.tzinfo)
    if now.time() < e:
        return datetime.combine(today - timedelta(days=1), s, now.tzinfo), datetime.combine(today, e, now.tzinfo)
    return None


def hours(start: str, end: str) -> float:
    s, e = parse(start), parse(end)
    minutes = (e.hour * 60 + e.minute) - (s.hour * 60 + s.minute)
    return (minutes % (24 * 60)) / 60
