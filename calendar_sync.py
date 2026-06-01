"""
Apple Calendar (iCloud CalDAV) integration.
Uses caldav only for calendar discovery; all CRUD uses raw HTTP (requests)
to avoid iCloud's 412 bug on CalDAV REPORT requests.
"""

import asyncio
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import caldav
import icalendar
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlparse

_pool = ThreadPoolExecutor(max_workers=2)
_ICLOUD_URL = "https://caldav.icloud.com"


def _auth(username: str, password: str) -> HTTPBasicAuth:
    return HTTPBasicAuth(username, password)


def _event_url(calendar_url: str, uid: str) -> str:
    """Construct event URL from calendar URL and UID."""
    return calendar_url.rstrip("/") + "/" + uid + ".ics"


# ── Sync (blocking) implementations ──────────────────────────────────────────

def _list_calendars(username: str, password: str) -> list:
    client = caldav.DAVClient(url=_ICLOUD_URL, username=username, password=password)
    principal = client.principal()
    return [
        {"name": c.name or "Без названия", "url": str(c.url)}
        for c in principal.calendars()
        if c.name
    ]


def _create_event(username: str, password: str, calendar_url: str,
                  uid: str, summary: str, dtstart: datetime) -> str:
    """PUT event to iCloud. Returns the event URL."""
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

    event_url = _event_url(calendar_url, uid)
    resp = requests.put(
        event_url,
        data=cal.to_ical(),
        auth=_auth(username, password),
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        timeout=20,
    )
    resp.raise_for_status()
    return event_url


def _update_event(username: str, password: str, calendar_url: str,
                  uid: str, new_dtstart: datetime, event_href: str = ""):
    url = event_href or _event_url(calendar_url, uid)
    auth = _auth(username, password)

    resp = requests.get(url, auth=auth, timeout=20)
    if not resp.ok:
        return

    parsed = icalendar.Calendar.from_ical(resp.content)
    for comp in parsed.walk():
        if comp.name == "VEVENT":
            for key in ("DTSTART", "DTEND"):
                if key in comp:
                    del comp[key]
            comp.add("dtstart", new_dtstart)
            comp.add("dtend", new_dtstart)

    requests.put(
        url,
        data=parsed.to_ical(),
        auth=auth,
        headers={"Content-Type": "text/calendar; charset=utf-8"},
        timeout=20,
    )


def _delete_event(username: str, password: str, calendar_url: str,
                  uid: str, event_href: str = ""):
    url = event_href or _event_url(calendar_url, uid)
    resp = requests.delete(url, auth=_auth(username, password), timeout=20)
    if resp.status_code not in (200, 204, 404):
        resp.raise_for_status()


# ── Async wrappers ────────────────────────────────────────────────────────────

async def _run(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, func, *args)


async def list_calendars(username: str, password: str) -> list:
    return await _run(_list_calendars, username, password)


async def create_event(username: str, password: str, calendar_url: str,
                       uid: str, summary: str, dtstart: datetime) -> str:
    return await _run(_create_event, username, password, calendar_url, uid, summary, dtstart)


async def update_event(username: str, password: str, calendar_url: str,
                       uid: str, new_dtstart: datetime, event_href: str = ""):
    await _run(_update_event, username, password, calendar_url, uid, new_dtstart, event_href)


async def delete_event(username: str, password: str, calendar_url: str,
                       uid: str, event_href: str = ""):
    await _run(_delete_event, username, password, calendar_url, uid, event_href)


def new_uid() -> str:
    return str(uuid.uuid4())
