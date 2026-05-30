import json
import aiosqlite
from datetime import datetime
from typing import Optional

DB_PATH = "reminders.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    chat_id       INTEGER PRIMARY KEY,
    timezone      TEXT    DEFAULT 'Europe/Amsterdam',
    snooze_options TEXT   DEFAULT '[15, 30, 60, 1440]'
);

CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    body         TEXT    NOT NULL,
    remind_at    TEXT    NOT NULL,
    recurrence   TEXT,
    created_at   TEXT    DEFAULT (datetime('now')),
    done         INTEGER DEFAULT 0,
    last_sent_at TEXT,
    dismissed    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS allowed_users (
    chat_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    approved   INTEGER DEFAULT 0,
    added_at   TEXT    DEFAULT (datetime('now'))
);
"""


_MIGRATIONS = [
    "ALTER TABLE reminders ADD COLUMN last_message_id INTEGER",
    "ALTER TABLE reminders ADD COLUMN calendar_uid TEXT",
    "ALTER TABLE reminders ADD COLUMN google_event_id TEXT",
    "ALTER TABLE user_settings ADD COLUMN icloud_username TEXT",
    "ALTER TABLE user_settings ADD COLUMN icloud_app_password TEXT",
    "ALTER TABLE user_settings ADD COLUMN icloud_calendar_url TEXT",
    "ALTER TABLE user_settings ADD COLUMN google_access_token TEXT",
    "ALTER TABLE user_settings ADD COLUMN google_refresh_token TEXT",
    "ALTER TABLE user_settings ADD COLUMN google_calendar_id TEXT",
]


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        for migration in _MIGRATIONS:
            try:
                await db.execute(migration)
            except Exception:
                pass  # column already exists
        await db.commit()


# ── Settings ──────────────────────────────────────────────────────────────────

async def get_settings(chat_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM user_settings WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
    if row:
        return dict(row)
    return {"chat_id": chat_id, "timezone": "Europe/Amsterdam", "snooze_options": "[15, 30, 60, 1440]"}


async def save_timezone(chat_id: int, timezone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, timezone) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET timezone=excluded.timezone",
            (chat_id, timezone),
        )
        await db.commit()


async def save_snooze_options(chat_id: int, options: list):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, snooze_options) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET snooze_options=excluded.snooze_options",
            (chat_id, json.dumps(options)),
        )
        await db.commit()


# ── Reminders ─────────────────────────────────────────────────────────────────

async def add_reminder(chat_id: int, body: str, remind_at: datetime,
                       recurrence: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO reminders (chat_id, body, remind_at, recurrence) VALUES (?, ?, ?, ?)",
            (chat_id, body, remind_at.isoformat(), recurrence),
        )
        await db.commit()
        return cur.lastrowid


async def get_pending_reminders(chat_id: int, page: int = 0,
                                per_page: int = 5) -> tuple:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) FROM reminders WHERE chat_id=? AND done=0", (chat_id,)
        )
        total = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT id, body, remind_at, recurrence FROM reminders"
            " WHERE chat_id=? AND done=0 ORDER BY remind_at LIMIT ? OFFSET ?",
            (chat_id, per_page, page * per_page),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def get_reminder(reminder_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_due_reminders(now: datetime, chat_id: int) -> list:
    """Due and not yet sent (or user dismissed a snooze and it's due again)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, chat_id, body, recurrence, remind_at, last_message_id FROM reminders"
            " WHERE done=0 AND chat_id=? AND remind_at<=?"
            "   AND (last_sent_at IS NULL OR dismissed=1)",
            (chat_id, now.isoformat()),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_unseen_reminders(cutoff: datetime, chat_id: int) -> list:
    """Sent before `cutoff` and user never responded → re-fire."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, chat_id, body, recurrence, remind_at, last_message_id FROM reminders"
            " WHERE done=0 AND chat_id=? AND dismissed=0"
            "   AND last_sent_at IS NOT NULL AND last_sent_at<=?",
            (chat_id, cutoff.isoformat()),
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── User access management ────────────────────────────────────────────────────

async def is_user_allowed(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM allowed_users WHERE chat_id=? AND approved=1", (chat_id,)
        )
        return await cur.fetchone() is not None


async def add_pending_user(chat_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO allowed_users (chat_id, username, first_name, approved)"
            " VALUES (?, ?, ?, 0)",
            (chat_id, username, first_name),
        )
        await db.commit()


async def is_pending(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM allowed_users WHERE chat_id=? AND approved=0", (chat_id,)
        )
        return await cur.fetchone() is not None


async def approve_user(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE allowed_users SET approved=1 WHERE chat_id=?", (chat_id,))
        await db.commit()


async def deny_user(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM allowed_users WHERE chat_id=?", (chat_id,))
        await db.commit()


async def remove_user(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM allowed_users WHERE chat_id=?", (chat_id,))
        await db.commit()


async def get_approved_users() -> list:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT chat_id, username, first_name FROM allowed_users WHERE approved=1"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ── Calendar integration ──────────────────────────────────────────────────────

async def get_calendar_settings(chat_id: int) -> Optional[dict]:
    """Returns iCloud credentials if fully configured, else None."""
    s = await get_settings(chat_id)
    if s.get("icloud_username") and s.get("icloud_app_password") and s.get("icloud_calendar_url"):
        return {
            "username": s["icloud_username"],
            "password": s["icloud_app_password"],
            "calendar_url": s["icloud_calendar_url"],
        }
    return None


async def save_calendar_credentials(chat_id: int, username: str, password: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, icloud_username, icloud_app_password)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            "   icloud_username=excluded.icloud_username,"
            "   icloud_app_password=excluded.icloud_app_password,"
            "   icloud_calendar_url=NULL",
            (chat_id, username, password),
        )
        await db.commit()


async def save_calendar_url(chat_id: int, calendar_url: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, icloud_calendar_url) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET icloud_calendar_url=excluded.icloud_calendar_url",
            (chat_id, calendar_url),
        )
        await db.commit()


async def clear_calendar_settings(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_settings SET icloud_username=NULL, icloud_app_password=NULL,"
            " icloud_calendar_url=NULL WHERE chat_id=?",
            (chat_id,),
        )
        await db.commit()


async def set_reminder_calendar_uid(reminder_id: int, uid: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET calendar_uid=? WHERE id=?", (uid, reminder_id))
        await db.commit()


async def set_reminder_google_event_id(reminder_id: int, event_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET google_event_id=? WHERE id=?", (event_id, reminder_id))
        await db.commit()


async def get_google_settings(chat_id: int) -> Optional[dict]:
    s = await get_settings(chat_id)
    if s.get("google_access_token") and s.get("google_refresh_token") and s.get("google_calendar_id"):
        return {
            "access_token": s["google_access_token"],
            "refresh_token": s["google_refresh_token"],
            "calendar_id": s["google_calendar_id"],
        }
    return None


async def save_google_tokens(chat_id: int, access_token: str, refresh_token: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, google_access_token, google_refresh_token)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET"
            "   google_access_token=excluded.google_access_token,"
            "   google_refresh_token=excluded.google_refresh_token,"
            "   google_calendar_id=NULL",
            (chat_id, access_token, refresh_token),
        )
        await db.commit()


async def save_google_calendar_id(chat_id: int, calendar_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO user_settings (chat_id, google_calendar_id) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET google_calendar_id=excluded.google_calendar_id",
            (chat_id, calendar_id),
        )
        await db.commit()


async def clear_google_settings(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE user_settings SET google_access_token=NULL,"
            " google_refresh_token=NULL, google_calendar_id=NULL WHERE chat_id=?",
            (chat_id,),
        )
        await db.commit()


async def mark_sent(reminder_id: int, now: datetime, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET last_sent_at=?, dismissed=0, last_message_id=? WHERE id=?",
            (now.isoformat(), message_id, reminder_id),
        )
        await db.commit()


async def mark_dismissed(reminder_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET dismissed=1 WHERE id=?", (reminder_id,))
        await db.commit()


async def snooze_reminder(reminder_id: int, new_remind_at: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET remind_at=?, last_sent_at=NULL, dismissed=1 WHERE id=?",
            (new_remind_at.isoformat(), reminder_id),
        )
        await db.commit()


async def complete_reminder(reminder_id: int, next_remind_at: Optional[datetime]):
    async with aiosqlite.connect(DB_PATH) as db:
        if next_remind_at:
            await db.execute(
                "UPDATE reminders SET remind_at=?, last_sent_at=NULL, dismissed=0 WHERE id=?",
                (next_remind_at.isoformat(), reminder_id),
            )
        else:
            await db.execute("UPDATE reminders SET done=1 WHERE id=?", (reminder_id,))
        await db.commit()


async def delete_reminder(reminder_id: int, chat_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM reminders WHERE id=? AND chat_id=?", (reminder_id, chat_id)
        )
        await db.commit()
    return cur.rowcount > 0


async def update_reminder_body(reminder_id: int, body: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET body=? WHERE id=?", (body, reminder_id))
        await db.commit()


async def update_reminder_time(reminder_id: int, remind_at: datetime):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET remind_at=?, last_sent_at=NULL, dismissed=0 WHERE id=?",
            (remind_at.isoformat(), reminder_id),
        )
        await db.commit()
