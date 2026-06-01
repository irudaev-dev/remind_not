import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    BotCommand, BotCommandScopeChat, BotCommandScopeDefault,
    CallbackQuery, KeyboardButton, Message,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

import db
import calendar_sync
import google_calendar as gcal_mod
from keyboards import (
    SNOOZE_LABELS, settings_snooze_keyboard, snooze_keyboard,
    confirm_delete_keyboard, edit_choice_keyboard, list_keyboard,
    new_reminder_keyboard, user_approval_keyboard, users_list_keyboard,
    calendar_main_keyboard, calendar_list_keyboard,
    timezone_choice_keyboard, timezone_regions_keyboard, timezone_list_keyboard,
)
from reminder_parser import parse_reminder, parse_new_time
from recurrence import next_occurrence

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", "0"))
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

RECURRENCE_LABELS = {
    "daily": "🔁 каждый день",
    "weekly": "🔁 каждую неделю",
    "weekdays": "🔁 по будням",
    "monthly": "🔁 каждый месяц",
}


# ── FSM states ────────────────────────────────────────────────────────────────

class EditState(StatesGroup):
    waiting_choice = State()       # user pressed ✏️ but hasn't chosen Text/Time yet
    waiting_body = State()
    waiting_time = State()
    waiting_custom_snooze = State()
    waiting_reminder_time = State()
    waiting_snooze_time = State()  # custom time from notification button


class CalendarState(StatesGroup):
    # Apple
    waiting_email = State()
    waiting_password = State()
    selecting_apple_calendar = State()
    # Google
    waiting_google_code = State()
    selecting_google_calendar = State()


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_owner(chat_id: int) -> bool:
    return OWNER_CHAT_ID == 0 or chat_id == OWNER_CHAT_ID


async def has_access(chat_id: int) -> bool:
    return is_owner(chat_id) or await db.is_user_allowed(chat_id)


def user_display(user) -> str:
    if user.username:
        return f"@{user.username}"
    return user.first_name or str(user.id)


async def _cal_create(chat_id: int, reminder_id: int, body: str, remind_at: datetime):
    cal = await db.get_calendar_settings(chat_id)
    if not cal:
        return
    try:
        uid = calendar_sync.new_uid()
        href = await calendar_sync.create_event(cal["username"], cal["password"],
                                                 cal["calendar_url"], uid, body, remind_at)
        await db.set_reminder_calendar_uid(reminder_id, uid)
        if href:
            await db.set_reminder_calendar_href(reminder_id, href)
    except Exception as e:
        log.warning("Calendar create failed: %s", e)


async def _cal_update(chat_id: int, reminder_id: int, new_dt: datetime):
    cal = await db.get_calendar_settings(chat_id)
    if not cal:
        return
    reminder = await db.get_reminder(reminder_id)
    uid = reminder.get("calendar_uid") if reminder else None
    if not uid:
        return
    href = reminder.get("calendar_event_href", "") or ""
    try:
        await calendar_sync.update_event(cal["username"], cal["password"],
                                         cal["calendar_url"], uid, new_dt, href)
    except Exception as e:
        log.warning("Calendar update failed: %s", e)


async def _cal_delete(chat_id: int, reminder_id: int):
    """Fetch uid before DB deletion, then delete from calendar."""
    cal = await db.get_calendar_settings(chat_id)
    if not cal:
        return
    reminder = await db.get_reminder(reminder_id)
    uid = reminder.get("calendar_uid") if reminder else None
    if not uid:
        return
    try:
        await calendar_sync.delete_event(cal["username"], cal["password"],
                                         cal["calendar_url"], uid)
    except Exception as e:
        log.warning("Calendar delete failed: %s", e)


async def _gcal_create(chat_id: int, reminder_id: int, body: str, remind_at: datetime):
    gcal = await db.get_google_settings(chat_id)
    if not gcal or not GOOGLE_CLIENT_ID:
        return
    try:
        event_id = await gcal_mod.create_event(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
            gcal["access_token"], gcal["refresh_token"],
            gcal["calendar_id"], body, remind_at,
        )
        await db.set_reminder_google_event_id(reminder_id, event_id)
    except Exception as e:
        log.warning("Google create failed: %s", e)


async def _gcal_update(chat_id: int, reminder_id: int, new_dt: datetime):
    gcal = await db.get_google_settings(chat_id)
    if not gcal or not GOOGLE_CLIENT_ID:
        return
    reminder = await db.get_reminder(reminder_id)
    event_id = reminder.get("google_event_id") if reminder else None
    if not event_id:
        return
    try:
        await gcal_mod.update_event(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
            gcal["access_token"], gcal["refresh_token"],
            gcal["calendar_id"], event_id, new_dt,
        )
    except Exception as e:
        log.warning("Google update failed: %s", e)


async def _gcal_delete_id(chat_id: int, event_id: str):
    gcal = await db.get_google_settings(chat_id)
    if not gcal or not GOOGLE_CLIENT_ID or not event_id:
        return
    try:
        await gcal_mod.delete_event(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
            gcal["access_token"], gcal["refresh_token"],
            gcal["calendar_id"], event_id,
        )
    except Exception as e:
        log.warning("Google delete failed: %s", e)


async def _cal_delete_uid(chat_id: int, uid: str, href: str = ""):
    """Delete calendar event by href (preferred) or uid fallback."""
    cal = await db.get_calendar_settings(chat_id)
    if not cal or not uid:
        return
    try:
        await calendar_sync.delete_event(cal["username"], cal["password"],
                                         cal["calendar_url"], uid, href)
    except Exception as e:
        log.warning("Calendar delete failed: %s", e)


async def get_tz(chat_id: int) -> str:
    return (await db.get_settings(chat_id))["timezone"]


async def get_snooze_opts(chat_id: int) -> list[int]:
    return json.loads((await db.get_settings(chat_id))["snooze_options"])


def now_in(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz)).replace(tzinfo=None)


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")


# ── Scheduler ─────────────────────────────────────────────────────────────────

async def _send_reminder(r: dict):
    tz = await get_tz(r["chat_id"])
    opts = await get_snooze_opts(r["chat_id"])
    now = now_in(tz)
    rec_label = RECURRENCE_LABELS.get(r.get("recurrence"), "")
    text = f"🔔 <b>{r['body']}</b>{f'  {rec_label}' if rec_label else ''}"

    # Remove keyboard from previous notification (keep the text)
    if r.get("last_message_id"):
        try:
            await bot.edit_message_reply_markup(
                chat_id=r["chat_id"],
                message_id=r["last_message_id"],
                reply_markup=None,
            )
        except Exception:
            pass  # message too old or already edited — ignore

    try:
        sent = await bot.send_message(
            r["chat_id"], text, parse_mode="HTML",
            reply_markup=snooze_keyboard(r["id"], opts),
        )
        await db.mark_sent(r["id"], now, sent.message_id)
    except Exception as e:
        log.error("send_reminder %d failed: %s", r["id"], e)


async def check_reminders():
    if OWNER_CHAT_ID == 0:
        return
    all_users = [OWNER_CHAT_ID] + [u["chat_id"] for u in await db.get_approved_users()]
    for uid in all_users:
        tz = (await db.get_settings(uid))["timezone"]
        now = now_in(tz)
        cutoff = now - timedelta(minutes=15)
        for r in await db.get_due_reminders(now, uid):
            await _send_reminder(r)
        for r in await db.get_unseen_reminders(cutoff, uid):
            await _send_reminder(r)


# ── /start ────────────────────────────────────────────────────────────────────

_WELCOME = (
    "👋 Привет! Я бот-напоминалка.\n\n"

    "📝 <b>Как создать напоминание</b>\n"
    "Просто напиши что и когда — я разберу сам:\n"
    "• <i>Завтра в 10 купить молоко</i>\n"
    "• <i>Через 2 часа позвонить в банк</i>\n"
    "• <i>В пятницу в 18:00 встреча</i>\n"
    "• <i>Каждый день в 8 выпить таблетку</i>\n"
    "• <i>Зарядка</i> — я спрошу время отдельно\n\n"

    "⏰ <b>Когда напоминание сработает</b>\n"
    "Появятся кнопки — отложить на 15 мин, 30 мин, 1 час и т.д., или отметить выполненным. "
    "Если не нажать ничего, напомню повторно через 15 минут.\n\n"

    "🔁 <b>Повторяющиеся напоминания</b>\n"
    "Фразы «каждый день», «каждый понедельник», «по будням», «каждый месяц» — "
    "напоминание будет повторяться автоматически.\n\n"

    "📅 <b>Синхронизация с календарём</b>\n"
    "Подключи Apple Calendar или Google Calendar — напоминания будут появляться там автоматически, "
    "а при переносе через «Отложить» событие тоже сдвинется.\n\n"

    "<b>Команды:</b>\n"
    "/list — список активных напоминаний (редактировать, удалить)\n"
    "/settings — настроить кнопки «Отложить»\n"
    "/calendar — подключить Apple или Google Calendar\n"
    "/timezone — изменить часовой пояс (или отправь геолокацию)\n"
    "/cancel — отменить текущее действие"
)


@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if is_owner(msg.chat.id):
        await msg.answer(_WELCOME, parse_mode="HTML")
        return

    if await db.is_user_allowed(msg.chat.id):
        await msg.answer(_WELCOME, parse_mode="HTML")
        return

    if await db.is_pending(msg.chat.id):
        await msg.answer("⏳ Твой запрос уже отправлен, жди подтверждения.")
        return

    if OWNER_CHAT_ID == 0:
        await msg.answer(f"Твой chat ID: <code>{msg.chat.id}</code>", parse_mode="HTML")
        return

    # New user — notify owner
    username = msg.from_user.username or ""
    first_name = msg.from_user.first_name or ""
    await db.add_pending_user(msg.chat.id, username, first_name)
    await msg.answer("📨 Запрос на доступ отправлен владельцу бота. Ожидай подтверждения.")

    name = f"@{username}" if username else first_name or str(msg.chat.id)
    await bot.send_message(
        OWNER_CHAT_ID,
        f"🔔 <b>Запрос доступа к боту</b>\n\n"
        f"👤 {name}\n"
        f"🆔 <code>{msg.chat.id}</code>",
        parse_mode="HTML",
        reply_markup=user_approval_keyboard(msg.chat.id),
    )


# ── User approval callbacks ───────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("approve_user:"))
async def cb_approve_user(cq: CallbackQuery):
    if not is_owner(cq.message.chat.id):
        return
    user_id = int(cq.data.split(":")[1])
    await db.approve_user(user_id)
    await cq.message.edit_text(
        cq.message.text + "\n\n✅ Доступ разрешён", reply_markup=None
    )
    try:
        await bot.send_message(user_id, "✅ Доступ разрешён! Можешь пользоваться ботом.\n\n" + _WELCOME)
    except Exception:
        pass
    await cq.answer("Доступ разрешён")


@dp.callback_query(F.data.startswith("deny_user:"))
async def cb_deny_user(cq: CallbackQuery):
    if not is_owner(cq.message.chat.id):
        return
    user_id = int(cq.data.split(":")[1])
    await db.deny_user(user_id)
    await cq.message.edit_text(
        cq.message.text + "\n\n❌ В доступе отказано", reply_markup=None
    )
    try:
        await bot.send_message(user_id, "❌ В доступе отказано.")
    except Exception:
        pass
    await cq.answer("Отклонено")


@dp.callback_query(F.data.startswith("remove_user:"))
async def cb_remove_user(cq: CallbackQuery):
    if not is_owner(cq.message.chat.id):
        return
    user_id = int(cq.data.split(":")[1])
    await db.remove_user(user_id)
    await cq.answer("Пользователь удалён")
    # Refresh list
    users = await db.get_approved_users()
    if not users:
        await cq.message.edit_text("👥 Нет других пользователей.", reply_markup=None)
    else:
        await cq.message.edit_reply_markup(reply_markup=users_list_keyboard(users))


# ── /users ────────────────────────────────────────────────────────────────────

@dp.message(Command("users"))
async def cmd_users(msg: Message):
    if not is_owner(msg.chat.id):
        return
    users = await db.get_approved_users()
    if not users:
        await msg.answer("👥 Нет других пользователей.")
        return
    lines = ["👥 <b>Пользователи с доступом:</b>\n"]
    for u in users:
        name = f"@{u['username']}" if u["username"] else u["first_name"] or "—"
        lines.append(f"• {name}  <code>{u['chat_id']}</code>")
    lines.append("\nНажми 🗑 чтобы удалить пользователя:")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=users_list_keyboard(users))


# ── /calendar ─────────────────────────────────────────────────────────────────

async def _show_calendar_status(target, chat_id: int, edit: bool = False):
    has_apple = bool(await db.get_calendar_settings(chat_id))
    has_google = bool(await db.get_google_settings(chat_id))
    lines = ["📅 <b>Синхронизация с календарём</b>\n"]
    lines.append(f"🍎 Apple Calendar: {'✅ подключён' if has_apple else '❌ не подключён'}")
    lines.append(f"📅 Google Calendar: {'✅ подключён' if has_google else '❌ не подключён'}")
    text = "\n".join(lines)
    kb = calendar_main_keyboard(has_apple, has_google)
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("calendar"))
async def cmd_calendar(msg: Message):
    if not await has_access(msg.chat.id):
        return
    await _show_calendar_status(msg, msg.chat.id)


# ── Apple Calendar ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "cal_connect_apple")
async def cb_cal_connect_apple(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    await state.set_state(CalendarState.waiting_email)
    await cq.message.edit_text("📧 Введи Apple ID (email от iCloud):\n\n/cancel — отмена")
    await cq.answer()


@dp.message(StateFilter(CalendarState.waiting_email))
async def cal_receive_email(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    email = msg.text.strip() if msg.text else ""
    if "@" not in email:
        await msg.answer("Похоже это не email. Введи Apple ID:\n\n/cancel — отмена")
        return
    await state.update_data(email=email)
    await state.set_state(CalendarState.waiting_password)
    await msg.answer(
        "🔑 Введи <b>App-Specific Password</b>\n\n"
        "Получить: <a href='https://appleid.apple.com'>appleid.apple.com</a> → "
        "Вход и безопасность → Пароли для программ\n\n/cancel — отмена",
        parse_mode="HTML", disable_web_page_preview=True,
    )


@dp.message(StateFilter(CalendarState.waiting_password))
async def cal_receive_password(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    password = msg.text.strip() if msg.text else ""
    if not password:
        return
    data = await state.get_data()
    try:
        await msg.delete()
    except Exception:
        pass
    thinking = await msg.answer("⏳ Подключаюсь к iCloud...")
    try:
        calendars = await calendar_sync.list_calendars(data["email"], password)
    except Exception as e:
        await thinking.edit_text(f"❌ Ошибка: <code>{e}</code>\n\n/calendar — попробовать снова", parse_mode="HTML")
        await state.clear()
        return
    await db.save_calendar_credentials(msg.chat.id, data["email"], password)
    await state.update_data(calendars=calendars)
    await state.set_state(CalendarState.selecting_apple_calendar)
    await thinking.edit_text(
        f"✅ Подключено! Выбери календарь:",
        reply_markup=calendar_list_keyboard(calendars, prefix="apple_pick"),
    )


@dp.callback_query(F.data.startswith("apple_pick:"))
async def cb_apple_pick(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    idx = int(cq.data.split(":")[1])
    data = await state.get_data()
    chosen = data["calendars"][idx]
    await db.save_calendar_url(cq.message.chat.id, chosen["url"])
    await state.clear()
    await cq.message.edit_text(f"✅ Apple Calendar: <b>{chosen['name']}</b>", parse_mode="HTML")
    await cq.answer("Подключено!")


@dp.callback_query(F.data == "cal_apple_disconnect")
async def cb_apple_disconnect(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    await db.clear_calendar_settings(cq.message.chat.id)
    await _show_calendar_status(cq.message, cq.message.chat.id, edit=True)
    await cq.answer("Apple Calendar отключён")


# ── Google Calendar ───────────────────────────────────────────────────────────

@dp.callback_query(F.data == "cal_connect_google")
async def cb_cal_connect_google(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        await cq.answer("GOOGLE_CLIENT_ID не задан в .env", show_alert=True)
        return
    auth_url = gcal_mod.get_auth_url(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    await state.set_state(CalendarState.waiting_google_code)
    await cq.message.edit_text(
        "📅 <b>Подключение Google Calendar</b>\n\n"
        f"1. Открой эту ссылку в браузере:\n{auth_url}\n\n"
        "2. Войди в Google аккаунт и разреши доступ\n"
        "3. Скопируй код и отправь его сюда\n\n"
        "/cancel — отмена",
        parse_mode="HTML", disable_web_page_preview=True,
    )
    await cq.answer()


@dp.message(StateFilter(CalendarState.waiting_google_code))
async def cal_receive_google_code(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    code = msg.text.strip() if msg.text else ""
    if not code:
        return
    thinking = await msg.answer("⏳ Проверяю код...")
    try:
        tokens = await asyncio.get_event_loop().run_in_executor(
            None, gcal_mod.exchange_code, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, code
        )
    except Exception as e:
        await thinking.edit_text(f"❌ Неверный код: <code>{e}</code>\n\nПопробуй снова или /cancel", parse_mode="HTML")
        return
    await db.save_google_tokens(msg.chat.id, tokens["access_token"], tokens["refresh_token"])
    try:
        calendars = await gcal_mod.list_calendars(
            GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
            tokens["access_token"], tokens["refresh_token"],
        )
    except Exception as e:
        await thinking.edit_text(f"❌ Не удалось получить список календарей: <code>{e}</code>", parse_mode="HTML")
        await state.clear()
        return
    await state.update_data(gcalendars=calendars)
    await state.set_state(CalendarState.selecting_google_calendar)
    await thinking.edit_text(
        "✅ Авторизация успешна! Выбери календарь:",
        reply_markup=calendar_list_keyboard(calendars, prefix="google_pick"),
    )


@dp.callback_query(F.data.startswith("google_pick:"))
async def cb_google_pick(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    idx = int(cq.data.split(":")[1])
    data = await state.get_data()
    chosen = data["gcalendars"][idx]
    await db.save_google_calendar_id(cq.message.chat.id, chosen["id"])
    await state.clear()
    await cq.message.edit_text(f"✅ Google Calendar: <b>{chosen['name']}</b>", parse_mode="HTML")
    await cq.answer("Подключено!")


@dp.callback_query(F.data == "cal_google_disconnect")
async def cb_google_disconnect(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    await db.clear_google_settings(cq.message.chat.id)
    await _show_calendar_status(cq.message, cq.message.chat.id, edit=True)
    await cq.answer("Google Calendar отключён")


@dp.callback_query(F.data == "cal_cancel")
async def cb_cal_cancel(cq: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_calendar_status(cq.message, cq.message.chat.id, edit=True)
    await cq.answer("Отменено")


# ── /cancel ───────────────────────────────────────────────────────────────────

@dp.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    await state.clear()
    await msg.answer("Действие отменено.", reply_markup=ReplyKeyboardRemove())


# ── /list ─────────────────────────────────────────────────────────────────────

@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not await has_access(msg.chat.id):
        return
    await _show_list(msg.chat.id, send_new=True, reply_to=msg)


async def _show_list(chat_id: int, send_new: bool = False,
                     reply_to: Optional[Message] = None, edit_msg: Optional[Message] = None):
    reminders = await db.get_pending_reminders(chat_id)

    if not reminders:
        text = "📋 Нет активных напоминаний."
        kb = None
    else:
        lines = ["📋 <b>Напоминания:</b>\n"]
        for i, r in enumerate(reminders, start=1):
            rec = RECURRENCE_LABELS.get(r["recurrence"], "")
            lines.append(f"<b>#{i}</b>  {fmt_dt(r['remind_at'])}  {rec}\n{r['body']}\n")
        text = "\n".join(lines)
        kb = list_keyboard(reminders)

    if edit_msg:
        await edit_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
    elif reply_to:
        await reply_to.answer(text, parse_mode="HTML", reply_markup=kb)
    elif send_new:
        await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


# ── /settings ─────────────────────────────────────────────────────────────────

@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    if not await has_access(msg.chat.id):
        return
    opts = await get_snooze_opts(msg.chat.id)
    await msg.answer(
        "⚙️ <b>Кнопки «Отложить»</b>\nВыбери интервалы, которые будут показаны при напоминании:",
        parse_mode="HTML",
        reply_markup=settings_snooze_keyboard(opts),
    )


@dp.callback_query(F.data.startswith("toggle_snooze:"))
async def cb_toggle_snooze(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    minutes = int(cq.data.split(":")[1])
    opts = await get_snooze_opts(cq.message.chat.id)
    if minutes in opts:
        if len(opts) > 1:  # keep at least one option
            opts.remove(minutes)
    else:
        opts.append(minutes)
        opts.sort()
    await db.save_snooze_options(cq.message.chat.id, opts)
    await cq.message.edit_reply_markup(reply_markup=settings_snooze_keyboard(opts))
    await cq.answer()


@dp.callback_query(F.data == "settings_save")
async def cb_settings_save(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    opts = await get_snooze_opts(cq.message.chat.id)
    labels = ", ".join(SNOOZE_LABELS.get(m, f"{m} мин") for m in opts)
    await cq.message.edit_text(f"✅ Сохранено! Кнопки: {labels}", reply_markup=None)
    await cq.answer("Настройки сохранены")


@dp.callback_query(F.data == "snooze_add_custom")
async def cb_snooze_add_custom(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    await state.set_state(EditState.waiting_custom_snooze)
    await cq.message.edit_text(
        "⏱ Введи своё время в минутах (только число):\n\n"
        "Примеры: <code>25</code>   <code>90</code>   <code>240</code>\n\n"
        "/cancel для отмены",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.message(StateFilter(EditState.waiting_custom_snooze))
async def receive_custom_snooze(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    text = msg.text.strip() if msg.text else ""
    if not text.isdigit() or not (1 <= int(text) <= 10080):
        await msg.answer("Введи число от 1 до 10080 (минут). /cancel для отмены.")
        return
    minutes = int(text)
    opts = await get_snooze_opts(msg.chat.id)
    if minutes not in opts:
        opts.append(minutes)
        opts.sort()
        await db.save_snooze_options(msg.chat.id, opts)
    await state.clear()
    label = SNOOZE_LABELS.get(minutes, f"{minutes} мин")
    await msg.answer(
        f"✅ Добавлено: <b>{label}</b>\n\nТекущие кнопки: "
        + ", ".join(SNOOZE_LABELS.get(m, f"{m} мин") for m in opts),
        parse_mode="HTML",
    )


# ── /timezone ─────────────────────────────────────────────────────────────────

@dp.message(Command("timezone"))
async def cmd_timezone(msg: Message):
    if not await has_access(msg.chat.id):
        return
    tz = await get_tz(msg.chat.id)
    await msg.answer(
        f"🌍 Текущий часовой пояс: <b>{tz}</b>\n\nКак изменить?",
        parse_mode="HTML",
        reply_markup=timezone_choice_keyboard(),
    )


@dp.callback_query(F.data == "tz_geo")
async def cb_tz_geo(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📍 Поделиться геолокацией", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await cq.message.edit_reply_markup(reply_markup=None)
    await cq.message.answer("Отправь геолокацию — определю часовой пояс автоматически:", reply_markup=kb)
    await cq.answer()


@dp.callback_query(F.data == "tz_list")
async def cb_tz_list(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    await cq.message.edit_text("🌍 Выбери регион:", reply_markup=timezone_regions_keyboard())
    await cq.answer()


@dp.callback_query(F.data.startswith("tz_region:"))
async def cb_tz_region(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    region = cq.data.split(":")[1]
    await cq.message.edit_text("Выбери часовой пояс:", reply_markup=timezone_list_keyboard(region))
    await cq.answer()


@dp.callback_query(F.data == "tz_back")
async def cb_tz_back(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    tz = await get_tz(cq.message.chat.id)
    await cq.message.edit_text(
        f"🌍 Текущий часовой пояс: <b>{tz}</b>\n\nКак изменить?",
        parse_mode="HTML",
        reply_markup=timezone_choice_keyboard(),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("tz_set:"))
async def cb_tz_set(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    tz = cq.data.split(":", 1)[1]
    await db.save_timezone(cq.message.chat.id, tz)
    now_str = now_in(tz).strftime("%H:%M")
    await cq.message.edit_text(
        f"✅ Часовой пояс: <b>{tz}</b>\nТекущее время: {now_str}",
        parse_mode="HTML",
        reply_markup=None,
    )
    await cq.answer("Сохранено!")


@dp.message(F.location)
async def handle_location(msg: Message):
    if not await has_access(msg.chat.id):
        return
    try:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder()
        tz = tf.timezone_at(lat=msg.location.latitude, lng=msg.location.longitude)
    except ImportError:
        await msg.answer("Библиотека timezonefinder не установлена.", reply_markup=ReplyKeyboardRemove())
        return
    if not tz:
        await msg.answer("Не удалось определить часовой пояс по этой локации.", reply_markup=ReplyKeyboardRemove())
        return
    await db.save_timezone(msg.chat.id, tz)
    now_str = now_in(tz).strftime("%H:%M")
    await msg.answer(
        f"✅ Часовой пояс: <b>{tz}</b>\nТекущее время: {now_str}",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove(),
    )


# ── Snooze / Done callbacks ───────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("snooze:"))
async def cb_snooze(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    _, rid, mins = cq.data.split(":")
    reminder_id, minutes = int(rid), int(mins)

    reminder = await db.get_reminder(reminder_id)
    if not reminder or reminder["done"]:
        await cq.answer("Напоминание уже выполнено или удалено.")
        await cq.message.edit_reply_markup(reply_markup=None)
        return

    tz = await get_tz(cq.message.chat.id)
    new_time = now_in(tz) + timedelta(minutes=minutes)
    await db.snooze_reminder(reminder_id, new_time)
    asyncio.create_task(_cal_update(cq.message.chat.id, reminder_id, new_time))
    asyncio.create_task(_gcal_update(cq.message.chat.id, reminder_id, new_time))

    label = SNOOZE_LABELS.get(minutes, f"{minutes} мин")
    await cq.answer(f"Отложено на {label} ⏰")
    await cq.message.edit_text(
        cq.message.text + f"\n\n⏰ <i>Отложено на {label} ({fmt_dt(new_time.isoformat())})</i>",
        parse_mode="HTML",
        reply_markup=None,
    )


@dp.callback_query(F.data.startswith("done:"))
async def cb_done(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])

    reminder = await db.get_reminder(reminder_id)
    if not reminder or reminder["done"]:
        await cq.answer("Напоминание уже выполнено.")
        await cq.message.edit_reply_markup(reply_markup=None)
        return

    if reminder["recurrence"]:
        remind_at = datetime.fromisoformat(reminder["remind_at"])
        next_dt = next_occurrence(remind_at, reminder["recurrence"])
        await db.complete_reminder(reminder_id, next_dt)
        next_str = fmt_dt(next_dt.isoformat()) if next_dt else "—"
        await cq.answer("Готово! Следующее запланировано.")
        await cq.message.edit_text(
            cq.message.text + f"\n\n✅ <i>Выполнено. Следующее: {next_str}</i>",
            parse_mode="HTML",
            reply_markup=None,
        )
    else:
        await db.complete_reminder(reminder_id, None)
        await cq.answer("Выполнено! ✅")
        await cq.message.edit_text(
            cq.message.text + "\n\n✅ <i>Выполнено</i>",
            parse_mode="HTML",
            reply_markup=None,
        )


# ── Set custom snooze time from notification ──────────────────────────────────

@dp.callback_query(F.data.startswith("set_time:"))
async def cb_set_time(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    reminder = await db.get_reminder(reminder_id)
    if not reminder or reminder["done"]:
        await cq.answer("Напоминание уже выполнено.")
        await cq.message.edit_reply_markup(reply_markup=None)
        return
    await db.mark_dismissed(reminder_id)
    await cq.message.edit_reply_markup(reply_markup=None)
    await state.set_state(EditState.waiting_snooze_time)
    await state.update_data(snooze_reminder_id=reminder_id)
    await cq.message.answer(
        f"🕐 Введи время для напоминания <b>«{reminder['body']}»</b>\n\n"
        "Примеры: <i>18, 18:30, завтра в 10, через 2 часа, в пятницу</i>\n\n"
        "/cancel — отмена",
        parse_mode="HTML",
    )
    await cq.answer()


@dp.message(StateFilter(EditState.waiting_snooze_time))
async def receive_snooze_time(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    data = await state.get_data()
    reminder_id = data.get("snooze_reminder_id")
    tz = await get_tz(msg.chat.id)

    thinking = await msg.answer("⏳ Разбираю время...")
    try:
        result = await parse_new_time(msg.text, tz)
    except Exception as e:
        log.error("parse_new_time error: %s", e)
        await thinking.edit_text("Ошибка. Попробуй ещё раз или /cancel.")
        return

    if result.get("error") or not result.get("remind_at"):
        await thinking.edit_text(
            "Не понял время. Попробуй: <i>18, завтра в 10, через 2 часа</i>\n\n/cancel — отмена",
            parse_mode="HTML",
        )
        return

    await state.clear()
    new_dt = datetime.fromisoformat(result["remind_at"])
    await db.snooze_reminder(reminder_id, new_dt)
    asyncio.create_task(_cal_update(msg.chat.id, reminder_id, new_dt))
    asyncio.create_task(_gcal_update(msg.chat.id, reminder_id, new_dt))
    await thinking.edit_text(
        f"✅ Перенесено на <b>{fmt_dt(result['remind_at'])}</b>",
        parse_mode="HTML",
    )


# ── Delete callbacks ──────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    reminder = await db.get_reminder(reminder_id)
    if not reminder:
        await cq.answer("Напоминание не найдено.")
        return
    await cq.message.edit_text(
        f"🗑 Удалить напоминание?\n\n<b>{reminder['body']}</b>\n{fmt_dt(reminder['remind_at'])}",
        parse_mode="HTML",
        reply_markup=confirm_delete_keyboard(reminder_id),
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("confirm_delete:"))
async def cb_confirm_delete(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    # Grab calendar_uid BEFORE deletion so the async task can use it safely
    reminder = await db.get_reminder(reminder_id)
    cal_uid = reminder.get("calendar_uid") if reminder else None
    cal_href = reminder.get("calendar_event_href", "") if reminder else ""
    google_event_id = reminder.get("google_event_id") if reminder else None

    deleted = await db.delete_reminder(reminder_id, cq.message.chat.id)
    if deleted:
        if cal_uid:
            asyncio.create_task(_cal_delete_uid(cq.message.chat.id, cal_uid, cal_href))
        if google_event_id:
            asyncio.create_task(_gcal_delete_id(cq.message.chat.id, google_event_id))
        await cq.answer("Удалено")
        await _show_list(cq.message.chat.id, edit_msg=cq.message)
    else:
        await cq.message.edit_text("Напоминание не найдено.", reply_markup=None)
        await cq.answer()


@dp.callback_query(F.data.startswith("quick_delete:"))
async def cb_quick_delete(cq: CallbackQuery):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    reminder = await db.get_reminder(reminder_id)
    cal_uid = reminder.get("calendar_uid") if reminder else None
    cal_href = reminder.get("calendar_event_href", "") if reminder else ""
    google_event_id = reminder.get("google_event_id") if reminder else None
    await db.delete_reminder(reminder_id, cq.message.chat.id)
    if cal_uid:
        asyncio.create_task(_cal_delete_uid(cq.message.chat.id, cal_uid, cal_href))
    if google_event_id:
        asyncio.create_task(_gcal_delete_id(cq.message.chat.id, google_event_id))
    await cq.message.edit_text("✅ Напоминание отменено.", reply_markup=None)
    await cq.answer("Отменено")


@dp.callback_query(F.data.startswith("cancel_action:"))
async def cb_cancel_action(cq: CallbackQuery):
    await cq.message.delete()
    await _show_list(cq.message.chat.id, send_new=True)
    await cq.answer("Отменено")


# ── Edit callbacks ────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("edit:"))
async def cb_edit(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    reminder = await db.get_reminder(reminder_id)
    if not reminder:
        await cq.answer("Напоминание не найдено.")
        return
    await cq.message.edit_text(
        f"✏️ Что изменить в напоминании?\n\n"
        f"<b>{reminder['body']}</b>\n{fmt_dt(reminder['remind_at'])}",
        parse_mode="HTML",
        reply_markup=edit_choice_keyboard(reminder_id),
    )
    await state.set_state(EditState.waiting_choice)
    await cq.answer()


@dp.message(StateFilter(EditState.waiting_choice))
async def receive_edit_choice_text(msg: Message):
    await msg.answer(
        "Нажми кнопку выше: <b>Текст</b> или <b>Время</b>.\n/cancel — отмена",
        parse_mode="HTML",
    )


@dp.callback_query(F.data.startswith("edit_body:"))
async def cb_edit_body(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    await state.set_state(EditState.waiting_body)
    await state.update_data(reminder_id=reminder_id, list_msg_id=cq.message.message_id)
    await cq.message.edit_text(
        "📝 Напиши новый текст напоминания:\n(/cancel для отмены)",
        reply_markup=None,
    )
    await cq.answer()


@dp.callback_query(F.data.startswith("edit_time:"))
async def cb_edit_time(cq: CallbackQuery, state: FSMContext):
    if not await has_access(cq.message.chat.id):
        return
    reminder_id = int(cq.data.split(":")[1])
    await state.set_state(EditState.waiting_time)
    await state.update_data(reminder_id=reminder_id, list_msg_id=cq.message.message_id)
    await cq.message.edit_text(
        "🕐 Напиши новое время или дату:\n(например: «завтра в 15:00», «через 2 часа»)\n(/cancel для отмены)",
        reply_markup=None,
    )
    await cq.answer()


@dp.message(StateFilter(EditState.waiting_body))
async def receive_new_body(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    data = await state.get_data()
    await state.clear()
    await db.update_reminder_body(data["reminder_id"], msg.text.strip())
    await _show_list(msg.chat.id, send_new=True)


@dp.message(StateFilter(EditState.waiting_time))
async def receive_new_time(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    data = await state.get_data()
    tz = await get_tz(msg.chat.id)

    thinking = await msg.answer("⏳ Разбираю время...")
    try:
        result = await parse_new_time(msg.text, tz)
    except Exception:
        await thinking.edit_text("Не удалось разобрать время. Попробуй ещё раз или /cancel")
        return

    if result.get("error") or not result.get("remind_at"):
        await thinking.edit_text(
            f"Не понял время: {result.get('error', '')}\n\nПопробуй: «завтра в 15:00» или /cancel"
        )
        return

    await state.clear()
    new_dt = datetime.fromisoformat(result["remind_at"])
    await db.update_reminder_time(data["reminder_id"], new_dt)
    await _show_list(msg.chat.id, edit_msg=thinking)


# ── Main text handler — parse new reminder ────────────────────────────────────

@dp.message(StateFilter(default_state), F.text)
async def handle_text(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return

    tz = await get_tz(msg.chat.id)
    thinking = await msg.answer("⏳ Разбираю...")

    try:
        result = await parse_reminder(msg.text, tz)
    except Exception as e:
        log.error("parse_reminder error: %s", e)
        await thinking.edit_text("Ошибка при разборе. Попробуй ещё раз.")
        return

    # Date not found — ask for time, keep the body
    if result.get("error") == "no_date":
        body = result.get("body") or msg.text.strip()
        await state.set_state(EditState.waiting_reminder_time)
        await state.update_data(pending_body=body)
        await thinking.edit_text(
            f"📝 <b>{body}</b>\n\n"
            f"🕐 Когда напомнить?\n\n"
            f"Например: <i>завтра в 10, через 2 часа, в пятницу в 18:00</i>\n\n"
            f"/cancel — отмена",
            parse_mode="HTML",
        )
        return

    body = result["body"]
    recurrence = result.get("recurrence")
    remind_at = datetime.fromisoformat(result["remind_at"])
    rid = await db.add_reminder(msg.chat.id, body, remind_at, recurrence)
    asyncio.create_task(_cal_create(msg.chat.id, rid, body, remind_at))
    asyncio.create_task(_gcal_create(msg.chat.id, rid, body, remind_at))

    rec_label = RECURRENCE_LABELS.get(recurrence, "")
    await thinking.edit_text(
        f"✅ Напоминание сохранено!\n\n"
        f"📝 {body}\n"
        f"🕐 {fmt_dt(result['remind_at'])}"
        + (f"\n{rec_label}" if rec_label else ""),
        reply_markup=new_reminder_keyboard(rid),
    )


@dp.message(StateFilter(EditState.waiting_reminder_time))
async def receive_reminder_time(msg: Message, state: FSMContext):
    if not await has_access(msg.chat.id):
        return
    data = await state.get_data()
    body = data.get("pending_body", "")
    tz = await get_tz(msg.chat.id)

    thinking = await msg.answer("⏳ Разбираю время...")
    try:
        result = await parse_new_time(msg.text, tz)
    except Exception as e:
        log.error("parse_new_time error: %s", e)
        await thinking.edit_text("Ошибка. Попробуй ещё раз или /cancel.")
        return

    if result.get("error") or not result.get("remind_at"):
        await thinking.edit_text(
            "Не понял время. Попробуй:\n"
            "<i>завтра в 10, через 2 часа, в пятницу в 18:00</i>\n\n"
            "/cancel — отмена",
            parse_mode="HTML",
        )
        return

    await state.clear()
    remind_at = datetime.fromisoformat(result["remind_at"])
    rid = await db.add_reminder(msg.chat.id, body, remind_at)
    asyncio.create_task(_cal_create(msg.chat.id, rid, body, remind_at))
    asyncio.create_task(_gcal_create(msg.chat.id, rid, body, remind_at))
    await thinking.edit_text(
        f"✅ Напоминание сохранено!\n\n"
        f"📝 {body}\n"
        f"🕐 {fmt_dt(result['remind_at'])}",
        reply_markup=new_reminder_keyboard(rid),
    )


# ── Entry point ───────────────────────────────────────────────────────────────

async def set_menu():
    user_cmds = [
        BotCommand(command="list",     description="📋 Мои напоминания"),
        BotCommand(command="settings", description="⚙️ Настройка кнопок «Отложить»"),
        BotCommand(command="timezone", description="🌍 Изменить часовой пояс"),
        BotCommand(command="calendar", description="📅 Синхронизация с календарём"),
        BotCommand(command="cancel",   description="❌ Отменить текущее действие"),
        BotCommand(command="start",    description="👋 Начало работы / справка"),
    ]
    owner_cmds = user_cmds + [
        BotCommand(command="users", description="👥 Управление пользователями"),
    ]
    await bot.set_my_commands(user_cmds, scope=BotCommandScopeDefault())
    if OWNER_CHAT_ID:
        await bot.set_my_commands(owner_cmds, scope=BotCommandScopeChat(chat_id=OWNER_CHAT_ID))


async def main():
    await db.init_db()
    await set_menu()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", seconds=30)
    scheduler.start()

    log.info("Bot started. Owner: %s", OWNER_CHAT_ID or "NOT SET")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
