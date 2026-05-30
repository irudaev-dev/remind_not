import calendar
from datetime import datetime, timedelta
from typing import Optional


def next_occurrence(remind_at: datetime, recurrence: str) -> Optional[datetime]:
    if not recurrence:
        return None
    if recurrence == "daily":
        return remind_at + timedelta(days=1)
    if recurrence == "weekly":
        return remind_at + timedelta(weeks=1)
    if recurrence == "weekdays":
        nxt = remind_at + timedelta(days=1)
        while nxt.weekday() >= 5:  # skip Sat=5, Sun=6
            nxt += timedelta(days=1)
        return nxt
    if recurrence == "monthly":
        m = remind_at.month + 1
        y = remind_at.year
        if m > 12:
            m, y = 1, y + 1
        d = min(remind_at.day, calendar.monthrange(y, m)[1])
        return remind_at.replace(year=y, month=m, day=d)
    return None
