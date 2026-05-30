"""
Google Calendar integration via OAuth 2.0.
Sync ops are blocking and run in a thread pool.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

_pool = ThreadPoolExecutor(max_workers=2)
_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_REDIRECT = "urn:ietf:wg:oauth:2.0:oob"


def _make_flow(client_id: str, client_secret: str) -> Flow:
    return Flow.from_client_config(
        {"installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=_SCOPES,
        redirect_uri=_REDIRECT,
    )


def get_auth_url(client_id: str, client_secret: str) -> str:
    flow = _make_flow(client_id, client_secret)
    url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    return url


def exchange_code(client_id: str, client_secret: str, code: str) -> dict:
    """Exchange auth code → {access_token, refresh_token}."""
    flow = _make_flow(client_id, client_secret)
    flow.fetch_token(code=code.strip())
    c = flow.credentials
    return {"access_token": c.token, "refresh_token": c.refresh_token}


def _service(client_id: str, client_secret: str,
              access_token: str, refresh_token: str):
    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=_SCOPES,
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ── Sync implementations ──────────────────────────────────────────────────────

def _list_calendars(client_id, client_secret, access_token, refresh_token) -> list:
    svc = _service(client_id, client_secret, access_token, refresh_token)
    items = svc.calendarList().list().execute().get("items", [])
    return [
        {"name": c["summary"], "id": c["id"]}
        for c in items
        if c.get("accessRole") in ("owner", "writer")
    ]


def _event_body(summary: str, dtstart: datetime) -> dict:
    iso = dtstart.replace(tzinfo=None).isoformat()
    return {
        "summary": summary,
        "start": {"dateTime": iso, "timeZone": "UTC"},
        "end":   {"dateTime": iso, "timeZone": "UTC"},
    }


def _create_event(client_id, client_secret, access_token, refresh_token,
                  calendar_id: str, summary: str, dtstart: datetime) -> str:
    svc = _service(client_id, client_secret, access_token, refresh_token)
    result = svc.events().insert(
        calendarId=calendar_id, body=_event_body(summary, dtstart)
    ).execute()
    return result["id"]


def _update_event(client_id, client_secret, access_token, refresh_token,
                  calendar_id: str, event_id: str, new_dtstart: datetime):
    svc = _service(client_id, client_secret, access_token, refresh_token)
    svc.events().update(
        calendarId=calendar_id,
        eventId=event_id,
        body=_event_body("", new_dtstart),  # body will be patched below
    )
    # Use patch to only update time fields
    iso = new_dtstart.replace(tzinfo=None).isoformat()
    svc.events().patch(
        calendarId=calendar_id,
        eventId=event_id,
        body={
            "start": {"dateTime": iso, "timeZone": "UTC"},
            "end":   {"dateTime": iso, "timeZone": "UTC"},
        },
    ).execute()


def _delete_event(client_id, client_secret, access_token, refresh_token,
                  calendar_id: str, event_id: str):
    svc = _service(client_id, client_secret, access_token, refresh_token)
    svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()


# ── Async wrappers ────────────────────────────────────────────────────────────

async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(_pool, fn, *args)


async def list_calendars(client_id, client_secret, access_token, refresh_token) -> list:
    return await _run(_list_calendars, client_id, client_secret, access_token, refresh_token)


async def create_event(client_id, client_secret, access_token, refresh_token,
                       calendar_id: str, summary: str, dtstart: datetime) -> str:
    return await _run(_create_event, client_id, client_secret,
                      access_token, refresh_token, calendar_id, summary, dtstart)


async def update_event(client_id, client_secret, access_token, refresh_token,
                       calendar_id: str, event_id: str, new_dtstart: datetime):
    await _run(_update_event, client_id, client_secret,
               access_token, refresh_token, calendar_id, event_id, new_dtstart)


async def delete_event(client_id, client_secret, access_token, refresh_token,
                       calendar_id: str, event_id: str):
    await _run(_delete_event, client_id, client_secret,
               access_token, refresh_token, calendar_id, event_id)
