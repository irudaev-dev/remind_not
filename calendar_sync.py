"""
Apple Calendar (iCloud CalDAV) integration.
All network calls are synchronous (caldav library) and run in a thread executor.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import caldav
import icalendar
from urllib.parse import urlparse

_pool = ThreadPoolExecutor(max_workers=2)
_ICLOUD_URL = "https://caldav.icloud.com"


def _discovery_client(username: str, password: str) -> caldav.DAVClient:
    """Client for principal/calendar discovery — uses main iCloud URL."""
    return caldav.DAVClient(url=_ICLOUD_URL, username=username, password=password)


def _calendar_client(username: str, password: str, calendar_url: str) -> caldav.Calendar:
    """
    iCloud redirects to a user-specific server (p12X-caldav.icloud.com).
    We must use that actual host as the client base, not the generic icloud.com.
    """
    parsed = urlparse(calendar_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base, username=username, password=password)
    return client.calendar(url=calendar_url)


# ── Sync (blocking) implementations ──────────────────────────────────────────

def _list_calendars(username: str, password: str) -> list:
    principal = _discovery_client(username, password).principal()
    return [
        {"name": c.name or "Без названия", "url": str(c.url)}
        for c in principal.calendars()
        if c.name
    ]


def _create_event(username: str, password: str, calendar_url: str,
                  uid: str, summary: str, dtstart: datetime) -> str:
    """Returns the event href (URL path) for later deletion."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//ReminderBot//EN")
    cal.add("version", "2.0")

    ev = icalendar.Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", dtstart)
    ev.add("dtend", dtstart)
    ev.add("dtstamp", datetime.utcnow())
    cal.add_component(ev)

    calendar = _calendar_client(username, password, calendar_url)
    result = calendar.add_event(cal.to_ical().decode("utf-8"))
    return str(result.url)


def _update_event(username: str, password: str, calendar_url: str,
                  uid: str, new_dtstart: datetime):
    calendar = _calendar_client(username, password, calendar_url)
    try:
        obj = calendar.event_by_uid(uid)
    except Exception:
        results = calendar.search(uid=uid)
        if not results:
            return
        obj = results[0]
    raw = obj.data
    if isinstance(raw, str):
        raw = raw.encode()

    parsed = icalendar.Calendar.from_ical(raw)
    for comp in parsed.walk():
        if comp.name == "VEVENT":
            for key in ("DTSTART", "DTEND"):
                if key in comp:
                    del comp[key]
            comp.add("dtstart", new_dtstart)
            comp.add("dtend", new_dtstart)

    obj.data = parsed.to_ical()
    obj.save()


def _delete_event(username: str, password: str, calendar_url: str,
                  uid: str, event_href: str = ""):
    """Delete by direct URL if href is known; otherwise fall back to uid search."""
    parsed = urlparse(calendar_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    client = caldav.DAVClient(url=base, username=username, password=password)

    if event_href:
        try:
            event = caldav.CalendarObjectResource(client=client, url=event_href)
            event.delete()
            return
        except Exception:
            pass

    # Fallback: iterate all events and find by UID (avoids REPORT/412)
    calendar = _calendar_client(username, password, calendar_url)
    for event in calendar.objects():
        try:
            raw = event.data
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            parsed_cal = icalendar.Calendar.from_ical(raw)
            for comp in parsed_cal.walk():
                if comp.name == "VEVENT" and str(comp.get("uid", "")) == uid:
                    event.delete()
                    return
        except Exception:
            continue


# ── Async wrappers ────────────────────────────────────────────────────────────

async def _run(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, func, *args)


async def list_calendars(username: str, password: str) -> list:
    return await _run(_list_calendars, username, password)


async def create_event(username: str, password: str, calendar_url: str,
                       uid: str, summary: str, dtstart: datetime) -> str:
    """Returns the event href URL."""
    return await _run(_create_event, username, password, calendar_url, uid, summary, dtstart)


async def update_event(username: str, password: str, calendar_url: str,
                       uid: str, new_dtstart: datetime):
    await _run(_update_event, username, password, calendar_url, uid, new_dtstart)


async def delete_event(username: str, password: str, calendar_url: str,
                       uid: str, event_href: str = ""):
    await _run(_delete_event, username, password, calendar_url, uid, event_href)


def new_uid() -> str:
    return str(uuid.uuid4())
