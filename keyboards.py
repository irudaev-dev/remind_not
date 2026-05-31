from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ── Timezone data ──────────────────────────────────────────────────────────────

TZ_REGIONS = {
    "europe": ("🌍 Европа", [
        ("UTC",               "UTC±0"),
        ("Europe/London",     "Лондон  +0/+1"),
        ("Europe/Lisbon",     "Лиссабон  +0/+1"),
        ("Europe/Paris",      "Париж  +1/+2"),
        ("Europe/Amsterdam",  "Амстердам  +1/+2"),
        ("Europe/Berlin",     "Берлин  +1/+2"),
        ("Europe/Rome",       "Рим  +1/+2"),
        ("Europe/Madrid",     "Мадрид  +1/+2"),
        ("Europe/Warsaw",     "Варшава  +1/+2"),
        ("Europe/Prague",     "Прага  +1/+2"),
        ("Europe/Vienna",     "Вена  +1/+2"),
        ("Europe/Stockholm",  "Стокгольм  +1/+2"),
        ("Europe/Zurich",     "Цюрих  +1/+2"),
        ("Europe/Bucharest",  "Бухарест  +2/+3"),
        ("Europe/Helsinki",   "Хельсинки  +2/+3"),
        ("Europe/Kyiv",       "Киев  +2/+3"),
        ("Europe/Athens",     "Афины  +2/+3"),
        ("Europe/Istanbul",   "Стамбул  +3"),
        ("Europe/Moscow",     "Москва  +3"),
        ("Europe/Samara",     "Самара  +4"),
    ]),
    "russia": ("🇷🇺 Россия", [
        ("Europe/Moscow",        "Москва  +3"),
        ("Europe/Samara",        "Самара  +4"),
        ("Asia/Yekaterinburg",   "Екатеринбург  +5"),
        ("Asia/Omsk",            "Омск  +6"),
        ("Asia/Novosibirsk",     "Новосибирск  +7"),
        ("Asia/Krasnoyarsk",     "Красноярск  +7"),
        ("Asia/Irkutsk",         "Иркутск  +8"),
        ("Asia/Yakutsk",         "Якутск  +9"),
        ("Asia/Vladivostok",     "Владивосток  +10"),
        ("Asia/Magadan",         "Магадан  +11"),
        ("Asia/Kamchatka",       "Камчатка  +12"),
    ]),
    "asia": ("🌏 Азия", [
        ("Asia/Dubai",      "Дубай  +4"),
        ("Asia/Almaty",     "Алматы  +5"),
        ("Asia/Kolkata",    "Мумбай  +5:30"),
        ("Asia/Dhaka",      "Дакка  +6"),
        ("Asia/Bangkok",    "Бангкок  +7"),
        ("Asia/Singapore",  "Сингапур  +8"),
        ("Asia/Shanghai",   "Пекин  +8"),
        ("Asia/Hong_Kong",  "Гонконг  +8"),
        ("Asia/Seoul",      "Сеул  +9"),
        ("Asia/Tokyo",      "Токио  +9"),
    ]),
    "america": ("🌎 Америка", [
        ("America/New_York",     "Нью-Йорк  -5/-4"),
        ("America/Chicago",      "Чикаго  -6/-5"),
        ("America/Denver",       "Денвер  -7/-6"),
        ("America/Los_Angeles",  "Лос-Анджелес  -8/-7"),
        ("America/Toronto",      "Торонто  -5/-4"),
        ("America/Vancouver",    "Ванкувер  -8/-7"),
        ("America/Mexico_City",  "Мехико  -6/-5"),
        ("America/Sao_Paulo",    "Сан-Паулу  -3"),
        ("America/Buenos_Aires", "Буэнос-Айрес  -3"),
    ]),
    "other": ("🌐 Другие", [
        ("Africa/Cairo",      "Каир  +2/+3"),
        ("Africa/Nairobi",    "Найроби  +3"),
        ("Australia/Sydney",  "Сидней  +10/+11"),
        ("Australia/Perth",   "Перт  +8"),
        ("Pacific/Auckland",  "Окленд  +12/+13"),
        ("Pacific/Honolulu",  "Гонолулу  -10"),
    ]),
}

# All available snooze intervals (minutes) and their labels
SNOOZE_LABELS: dict[int, str] = {
    5: "5 мин", 10: "10 мин", 15: "15 мин", 20: "20 мин",
    30: "30 мин", 45: "45 мин", 60: "1 час", 90: "1.5 ч",
    120: "2 часа", 180: "3 часа", 360: "6 ч", 720: "12 ч",
    1440: "1 день", 2880: "2 дня", 10080: "1 нед.",
}
ALL_SNOOZE_OPTIONS = list(SNOOZE_LABELS.keys())


def _rows(buttons: list, per_row: int) -> list[list]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def snooze_keyboard(reminder_id: int, snooze_options: list[int]) -> InlineKeyboardMarkup:
    btns = [
        InlineKeyboardButton(
            text=f"⏰ {SNOOZE_LABELS.get(m, f'{m} мин')}",
            callback_data=f"snooze:{reminder_id}:{m}",
        )
        for m in snooze_options
    ]
    rows = _rows(btns, 3)
    rows.append([InlineKeyboardButton(text="🕐 Своё время", callback_data=f"set_time:{reminder_id}")])
    rows.append([InlineKeyboardButton(text="✅ Сделано",    callback_data=f"done:{reminder_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timezone_choice_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📍 Определить по геолокации", callback_data="tz_geo")],
        [InlineKeyboardButton(text="🌍 Выбрать из списка",        callback_data="tz_list")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timezone_regions_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"tz_region:{key}")]
        for key, (label, _) in TZ_REGIONS.items()
    ]
    rows.append([InlineKeyboardButton(text="◀ Назад", callback_data="tz_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timezone_list_keyboard(region_key: str) -> InlineKeyboardMarkup:
    _, zones = TZ_REGIONS[region_key]
    rows = _rows([
        InlineKeyboardButton(text=label, callback_data=f"tz_set:{tz}")
        for tz, label in zones
    ], 2)
    rows.append([InlineKeyboardButton(text="◀ Назад к регионам", callback_data="tz_list")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calendar_main_keyboard(has_apple: bool, has_google: bool) -> InlineKeyboardMarkup:
    rows = []
    if has_apple:
        rows.append([InlineKeyboardButton(text="🍎 Apple Calendar ✅ — отключить", callback_data="cal_apple_disconnect")])
    else:
        rows.append([InlineKeyboardButton(text="🍎 Подключить Apple Calendar", callback_data="cal_connect_apple")])
    if has_google:
        rows.append([InlineKeyboardButton(text="📅 Google Calendar ✅ — отключить", callback_data="cal_google_disconnect")])
    else:
        rows.append([InlineKeyboardButton(text="📅 Подключить Google Calendar", callback_data="cal_connect_google")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calendar_list_keyboard(calendars: list, prefix: str = "cal_pick") -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"📅 {c['name']}", callback_data=f"{prefix}:{i}")]
        for i, c in enumerate(calendars)
    ]
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cal_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_approval_keyboard(user_chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Разрешить", callback_data=f"approve_user:{user_chat_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"deny_user:{user_chat_id}"),
    ]])


def users_list_keyboard(users: list) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(
            text=f"🗑 {u['first_name'] or u['username'] or u['chat_id']}",
            callback_data=f"remove_user:{u['chat_id']}",
        )
    ] for u in users]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def new_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Изменить время", callback_data=f"edit_time:{reminder_id}"),
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"delete:{reminder_id}"),
    ]])


def confirm_delete_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"confirm_delete:{reminder_id}"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_action:{reminder_id}"),
    ]])


def edit_choice_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Текст", callback_data=f"edit_body:{reminder_id}"),
            InlineKeyboardButton(text="🕐 Время", callback_data=f"edit_time:{reminder_id}"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"cancel_action:{reminder_id}")],
    ])


def list_keyboard(reminders: list) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=f"✏️ {i}", callback_data=f"edit:{r['id']}"),
            InlineKeyboardButton(text=f"🗑 {i}", callback_data=f"delete:{r['id']}"),
        ]
        for i, r in enumerate(reminders, start=1)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_snooze_keyboard(current_options: list) -> InlineKeyboardMarkup:
    # Show preset options + any custom ones not in the preset list
    all_opts = sorted(set(ALL_SNOOZE_OPTIONS) | set(current_options))
    btns = [
        InlineKeyboardButton(
            text=f"{'✅' if m in current_options else '⬜'} {SNOOZE_LABELS.get(m, f'{m} мин')}",
            callback_data=f"toggle_snooze:{m}",
        )
        for m in all_opts
    ]
    rows = _rows(btns, 3)
    rows.append([
        InlineKeyboardButton(text="➕ Добавить своё время", callback_data="snooze_add_custom"),
        InlineKeyboardButton(text="💾 Сохранить", callback_data="settings_save"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
