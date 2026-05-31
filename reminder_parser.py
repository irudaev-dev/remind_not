import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates

# ── Detect explicit time in message ──────────────────────────────────────────
# If none of these patterns found → no time specified → use current time as default

_HAS_TIME_RE = re.compile(
    r'('
    r'через\s+\d+\s+(?:минут|час)'              # "через 2 часа"
    r'|\d{1,2}[:.]\d{2}'                         # "18:30" / "18.30"
    r'|в\s+\d{1,2}(?:[:.]\d{2})?'               # "в 18" / "в 18:00"
    r'|\d{1,2}\s*(?:утра|вечера|дня|ночи)'       # "9 утра"
    r'|утром|вечером|ночью|днём|днем'            # time-of-day words
    r'|in\s+\d+\s+(?:minute|hour)'              # "in 2 hours"
    r'|at\s+\d{1,2}'                             # "at 18"
    r'|(?:сегодня|завтра|послезавтра|today|tomorrow)\s+\d{1,2}\b'  # "сегодня 17" / "завтра 18"
    r')',
    # Also check normalized text (bare number → "в HH:00")

    re.I | re.U,
)

# ── Trigger words ─────────────────────────────────────────────────────────────

_TRIGGER_RE = re.compile(
    r'(^|\b)(напомни(те)?(\s+мне)?|remind(\s+me)?'
    r'|не\s+забудь|поставь\s+напоминание|запомни)(\b|$)',
    re.I | re.U,
)

# ── Recurrence ────────────────────────────────────────────────────────────────

_RECURRENCE: list = [
    (re.compile(r'\b(каждый день|ежедневно|каждое утро|каждый вечер|every day|daily)\b', re.I), 'daily'),
    (re.compile(r'\b(каждый\s+(понедельник|вторник|среду?|четверг|пятницу|субботу|воскресенье))\b', re.I), 'weekly'),
    (re.compile(r'\b(every\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b', re.I), 'weekly'),
    (re.compile(r'\b(каждую неделю|еженедельно|every week|weekly)\b', re.I), 'weekly'),
    (re.compile(r'\b(по будням|по рабочим дням|weekdays)\b', re.I), 'weekdays'),
    (re.compile(r'\b(каждый месяц|ежемесячно|every month|monthly)\b', re.I), 'monthly'),
]

# Strip these words before date parsing so they don't confuse dateparser
_RECURRENCE_STRIP_RE = re.compile(
    r'\b(каждый|каждую|каждое|ежедневно|еженедельно|ежемесячно'
    r'|по\s+будням|по\s+рабочим\s+дням|daily|weekly|monthly|weekdays)\b(\s+\w+)?',
    re.I | re.U,
)

# ── Preprocessing ─────────────────────────────────────────────────────────────

# "в 11" → "в 11:00", but not "в 11:30" (already has minutes)
_BARE_HOUR_RE = re.compile(r'\bв\s+(\d{1,2})\b(?!\s*[:.]\d)', re.U)
# Bare number 0-23 at the end of any text: "забрать 19" → time 19:00
_TRAILING_BARE_HOUR_RE = re.compile(r'(?<![:.0-9])(\d{1,2})\s*$', re.U)

# "завтра 18" / "в пятницу 9" → insert "в" before bare hour after day word
_DAY_HOUR_RE = re.compile(
    r'\b(сегодня|завтра|послезавтра|понедельник|вторник|среду?|четверг|пятницу|субботу|воскресенье'
    r'|today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)'
    r'\s+(\d{1,2})\b(?!\s*[:.]\d)',
    re.I | re.U,
)

# "утром"→"09:00", "днём/днем"→"13:00", "вечером"→"18:00", "ночью"→"22:00"
_TIME_WORDS = {
    'утром': '09:00', 'утра': '09:00',
    'днём': '13:00', 'днем': '13:00', 'дня': '13:00',
    'вечером': '18:00', 'вечера': '18:00',
    'ночью': '22:00', 'ночи': '22:00',
}
_TIME_WORD_RE = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _TIME_WORDS) + r')\b', re.I | re.U
)


def _normalize(text: str) -> str:
    """Make time patterns explicit so dateparser recognises them."""
    # Bare number 0-23 as entire input → treat as hour: "18" → "в 18:00"
    stripped = text.strip()
    if re.match(r'^\d{1,2}$', stripped):
        h = int(stripped)
        if 0 <= h <= 23:
            return f'в {h:02d}:00'

    # "завтра 18" → "завтра в 18:00"
    text = _DAY_HOUR_RE.sub(lambda m: f'{m.group(1)} в {int(m.group(2)):02d}:00', text)
    # "в 11" → "в 11:00"
    text = _BARE_HOUR_RE.sub(lambda m: f'в {int(m.group(1)):02d}:00', text)
    # "утром" → "09:00" etc. (only when standalone, not after "в")
    def _replace_word(m):
        prev = text[:m.start()].rstrip()
        if prev.endswith('в'):
            return m.group(0)
        return _TIME_WORDS[m.group(1).lower()]
    text = _TIME_WORD_RE.sub(_replace_word, text)
    # Trailing bare hour: "забрать 19" → "забрать в 19:00"
    m = _TRAILING_BARE_HOUR_RE.search(text)
    if m:
        h = int(m.group(1))
        if 0 <= h <= 23 and not re.search(r'в\s*$', text[:m.start()].rstrip()):
            text = text[:m.start()].rstrip() + f' в {h:02d}:00'
    return text


def _for_parsing(text: str) -> str:
    """Strip recurrence words so dateparser can find the date/time."""
    return _RECURRENCE_STRIP_RE.sub(' ', text).strip()

# ── Date phrase removal (for extracting body) ─────────────────────────────────

_DATE_RE = re.compile(
    r'\b('
    r'через\s+\d+(?:[,\.]\d+)?\s+(?:минут[уы]?|час[аов]?|дн[ейя]?|недел[юьи]|месяц[аев]?)'
    r'|сегодня(?:\s+(?:в\s+)?\d{1,2}(?:[:.]\d{2})?(?:\s*(?:утра|вечера|дня|ночи))?)?'
    r'|(?:после)?завтра(?:\s+(?:в\s+)?\d{1,2}(?:[:.]\d{2})?(?:\s*(?:утра|вечера|дня|ночи))?)?'
    r'|(?:следующ(?:ий|ую|ее)\s+)?(?:в\s+)?(?:понедельник|вторник|среду?|четверг|пятницу|субботу|воскресенье)(?:\s+в\s+\d{1,2}(?:[:.]\d{2})?(?:\s*(?:утра|вечера|дня|ночи))?)?'
    r'|в\s+\d{1,2}[:.]\d{2}(?:\s*(?:утра|вечера|дня|ночи))?'
    r'|в\s+\d{1,2}(?!\s*[:.]\d|[0-9])(?:\s*(?:утра|вечера|дня|ночи))?'
    r'|\d{1,2}[:.]\d{2}'
    r'|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+в\s+\d{1,2}(?:[:.]\d{2})?)?'
    r'|каждый(?:ую|ое)?\s+\w+|ежедневно|еженедельно|ежемесячно|по\s+будням|по\s+рабочим\s+дням'
    r'|in\s+\d+\s+(?:minutes?|hours?|days?|weeks?|months?)'
    r'|tomorrow(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?'
    r'|on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)'
    r'|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?'
    r'|every\s+\w+'
    r'|daily|weekly|monthly|weekdays'
    r'|(?<![:.0-9])\b(?:[01]?\d|2[0-3])\b(?=\s*$)'  # trailing bare hour "забрать 19"
    r')\b',
    re.I | re.U,
)

# ── Combining multiple search_dates results ───────────────────────────────────

_DAY_FRAG_RE = re.compile(
    r'(завтра|послезавтра|понедельник|вторник|среда|четверг|пятниц|суббот|воскресень'
    r'|monday|tuesday|wednesday|thursday|friday|saturday|sunday)',
    re.I,
)
_TIME_FRAG_RE = re.compile(r'(\d[:.]\d|в\s+\d|at\s+\d)', re.I)


def _combine(results: list) -> Optional[datetime]:
    if not results:
        return None
    if len(results) == 1:
        return results[0][1]
    day_dt = time_dt = None
    for frag, dt in results:
        if _DAY_FRAG_RE.search(frag):
            day_dt = dt
        if _TIME_FRAG_RE.search(frag):
            time_dt = dt
    if day_dt and time_dt:
        return day_dt.replace(hour=time_dt.hour, minute=time_dt.minute, second=0)
    return max(results, key=lambda x: x[1])[1]


def _clean_body(original: str, date_frags: list) -> str:
    text = original
    for frag in date_frags:
        text = text.replace(frag, ' ')
    text = _TRIGGER_RE.sub(' ', text)
    text = _DATE_RE.sub(' ', text)
    text = _RECURRENCE_STRIP_RE.sub(' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' ,.:;-–—')
    return (text[0].upper() + text[1:]) if text else ''


def _detect_recurrence(text: str) -> Optional[str]:
    for pattern, label in _RECURRENCE:
        if pattern.search(text):
            return label
    return None


def _dp_settings(timezone: str) -> dict:
    return {
        'TIMEZONE': timezone,
        'RETURN_AS_TIMEZONE_AWARE': False,
        'PREFER_DATES_FROM': 'future',
    }

# ── Public API ────────────────────────────────────────────────────────────────

async def parse_reminder(text: str, timezone: str) -> dict:
    settings = _dp_settings(timezone)
    normalized = _normalize(text)
    stripped = _for_parsing(normalized)

    results = search_dates(stripped, languages=['ru', 'en'], settings=settings) or []
    dt = _combine(results)

    if not dt:
        dt = dateparser.parse(stripped, languages=['ru', 'en'], settings=settings)
    if not dt:
        dt = dateparser.parse(normalized, languages=['ru', 'en'], settings=settings)

    # Always extract body — even when no date found (caller can ask for time separately)
    date_frags = [frag for frag, _ in results]
    body = _clean_body(text, date_frags) or _clean_body(text, [])
    body = body or text.strip()

    if not dt:
        return {
            'body': body,
            'remind_at': None,
            'recurrence': None,
            'error': 'no_date',
        }

    # No explicit time in message → use current time instead of dateparser default (midnight)
    # Check both original and normalized ("18" → "в 18:00" is still explicit time)
    if not _HAS_TIME_RE.search(text) and not _HAS_TIME_RE.search(normalized):
        now = datetime.now(ZoneInfo(timezone)).replace(tzinfo=None)
        dt = dt.replace(hour=now.hour, minute=now.minute, second=0)

    return {
        'body': body,
        'remind_at': dt.isoformat(),
        'recurrence': _detect_recurrence(text),
        'error': None,
    }


async def parse_new_time(text: str, timezone: str) -> dict:
    settings = _dp_settings(timezone)
    normalized = _normalize(text)
    results = search_dates(normalized, languages=['ru', 'en'], settings=settings) or []
    dt = _combine(results) or dateparser.parse(normalized, languages=['ru', 'en'], settings=settings)

    if not dt:
        return {'remind_at': None, 'error': 'Не могу найти дату/время'}

    return {'remind_at': dt.isoformat(), 'error': None}
