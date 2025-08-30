# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — версия для Render (webhook с fallback на polling)

Добавлено:
- Статистика подписок/отписок по источникам:
  /subs_all — за всё время
  /subs_month [YYYY-MM] — за месяц (текущий, если не указан)
  /subs_refresh — обновить UnsubscribedAt по факту (админ)
  /subs_menu — инлайн-меню для выбора периода (только админ):
      🗓 Текущий месяц, ⏮ Прошлый месяц, 📆 Выбрать месяц, ∞ Всё время
"""

import os, random, string, calendar
from datetime import datetime, timedelta
from typing import Dict, Set, List, Tuple, Optional

import telebot
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # например: @sbalo_channel
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
STAFF_IDS: Set[int] = set(int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip().isdigit())
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUBSCRIPTION_MIN_DAYS = int(os.getenv("SUBSCRIPTION_MIN_DAYS", "0"))
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
DISCOUNT_LABEL = os.getenv("DISCOUNT_LABEL", "7%")

if not SERVICE_ACCOUNT_JSON:
    raise SystemExit("ENV SERVICE_ACCOUNT_JSON пуст — вставьте содержимое credentials.json в переменную окружения.")

missing = [k for k, v in [("BOT_TOKEN", BOT_TOKEN),
                          ("CHANNEL_USERNAME", CHANNEL_USERNAME),
                          ("SPREADSHEET_ID", SPREADSHEET_ID)] if not v]
if missing:
    raise SystemExit("Нет переменных окружения: " + ", ".join(missing))

# ---------- Google Sheets ----------
CREDENTIALS_PATH = "/tmp/credentials.json"
with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
    f.write(SERVICE_ACCOUNT_JSON)

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES)
client = gspread.authorize(creds)

# Основной лист
sheet = client.open_by_key(SPREADSHEET_ID).sheet1
HEADERS = ["UserID","Username","PromoCode","DateIssued","DateRedeemed","RedeemedBy","OrderID","Source","SubscribedSince","Discount","UnsubscribedAt"]
headers = sheet.row_values(1)
if not headers:
    sheet.append_row(HEADERS)
    headers = HEADERS[:]
else:
    for h in HEADERS:
        if h not in headers:
            sheet.update_cell(1, len(headers) + 1, h)
            headers.append(h)

# Лист отзывов
try:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Feedback")
except gspread.WorksheetNotFound:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).add_worksheet(title="Feedback", rows=2000, cols=6)
    feedback_ws.append_row(["UserID","Username","Rating","Text","Photos","Date"])

# ---------- Telegram ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

STATE: Dict[int, str] = {}
USER_SOURCE: Dict[int, str] = {}
FEEDBACK_DRAFT: Dict[int, Dict] = {}

# ---------- Кнопки ----------
BTN_ABOUT = "ℹ️ О бренде"
BTN_FEEDBACK = "📝 Оставить отзыв"
BTN_STAFF_VERIFY = "✅ Проверить/Погасить код"
BTN_ADMIN_ADD_STAFF = "➕ Добавить сотрудника"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP_PHOTOS = "⏩ Пропустить фото"
BTN_SEND_FEEDBACK = "✅ Отправить"
RATING_BTNS = ["⭐ 1","⭐ 2","⭐ 3","⭐ 4","⭐ 5"]

# ---------- Права ----------
def is_admin(uid: int) -> bool:
    return bool(ADMIN_ID) and uid == ADMIN_ID

def is_staff(uid: int) -> bool:
    return uid in STAFF_IDS or is_admin(uid)

def add_staff_id(new_id: int) -> None:
    STAFF_IDS.add(new_id)
    os.environ["STAFF_IDS"] = ",".join(str(x) for x in sorted(STAFF_IDS))

# ---------- Клавиатуры ----------
def make_main_keyboard(user_id: int):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(telebot.types.KeyboardButton(BTN_ABOUT), telebot.types.KeyboardButton(BTN_FEEDBACK))
    if is_staff(user_id):
        kb.add(telebot.types.KeyboardButton(BTN_STAFF_VERIFY))
    if is_admin(user_id):
        kb.add(telebot.types.KeyboardButton(BTN_ADMIN_ADD_STAFF))
    return kb

def rating_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=5)
    kb.add(*[telebot.types.KeyboardButton(x) for x in RATING_BTNS])
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    return kb

def photos_keyboard():
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(telebot.types.KeyboardButton(BTN_SEND_FEEDBACK), telebot.types.KeyboardButton(BTN_SKIP_PHOTOS))
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    return kb

def inline_subscribe_keyboard():
    ikb = telebot.types.InlineKeyboardMarkup()
    ikb.add(telebot.types.InlineKeyboardButton("✅ Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    ikb.add(telebot.types.InlineKeyboardButton("🎁 Проверить подписку и получить промокод", callback_data="check_and_issue"))
    return ikb

# ---------- Тексты ----------
WELCOME = (
    "Добро пожаловать в <b>SBALO</b> 👠✨\n"
    "Здесь ты найдёшь вдохновение, узнаешь о новинках бренда и сможешь поделиться своим впечатлением.\n\n"
    "Выбирай кнопки снизу и будь ближе к миру SBALO."
)

BRAND_ABOUT = (
    "<b>SBALO</b> в переводе с итальянского означает «высшая мера удовольствия» — именно это мы хотим дарить каждому.\n\n"
    "Мы создаём обувь на фабриках в Стамбуле и Гуанчжоу, где производят коллекции мировые fashion-бренды.\n\n"
    "В наших коллекциях используются разные материалы, но особая часть моделей создаётся из итальянской кожи высшего качества. "
    "Она обладает уникальным свойством: через 1–2 дня носки обувь подстраивается под стопу и становится такой же удобной, как любимые тапочки.\n\n"
    "SBALO — это твой стиль и твой комфорт в каждом шаге."
)

# ---------- Sheets утилиты ----------
def append_row_dict(ws, header_list: List[str], data: dict):
    headers_now = ws.row_values(1)
    if not headers_now:
        ws.append_row(header_list)
        headers_now = header_list[:]
    row = [""] * len(headers_now)
    for k, v in data.items():
        if k in headers_now:
            row[headers_now.index(k)] = str(v)
    ws.append_row(row)

def get_row_by_user(user_id: int) -> Tuple[Optional[int], Optional[dict]]:
    for i, rec in enumerate(sheet.get_all_records(), start=2):
        if str(rec.get("UserID")) == str(user_id):
            return i, rec
    return None, None

def find_user_code(user_id: int) -> Tuple[Optional[int], Optional[str]]:
    i, rec = get_row_by_user(user_id)
    if i and rec.get("PromoCode"):
        return i, rec["PromoCode"]
    return None, None

def ensure_column(name: str):
    hdrs = sheet.row_values(1)
    if name not in hdrs:
        sheet.update_cell(1, len(hdrs) + 1, name)

# ---------- Промо/подписка ----------
def generate_short_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=4))
        if any(ch.isalpha() for ch in code):
            return code

def ensure_subscribed_since(user_id: int) -> datetime:
    i, rec = get_row_by_user(user_id)
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    ensure_column("SubscribedSince")
    if i and rec.get("SubscribedSince"):
        try:
            return datetime.fromisoformat(rec["SubscribedSince"])
        except Exception:
            pass
    if i:
        col = sheet.row_values(1).index("SubscribedSince") + 1
        sheet.update_cell(i, col, now)
    else:
        append_row_dict(sheet, HEADERS, {
            "UserID": str(user_id),
            "Source": "subscribe_check",
            "SubscribedSince": now
        })
    return datetime.fromisoformat(now)

def can_issue(user_id: int) -> bool:
    if SUBSCRIPTION_MIN_DAYS <= 0:
        return True
    since = ensure_subscribed_since(user_id)
    return (datetime.now() - since).days >= SUBSCRIPTION_MIN_DAYS

def issue_code(user_id: int, username: str, source: str = "subscribe") -> Tuple[str, bool]:
    _, existing = find_user_code(user_id)
    if existing:
        return existing, False
    code = generate_short_code()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    append_row_dict(sheet, HEADERS, {
        "UserID": str(user_id),
        "Username": username or "",
        "PromoCode": code,
        "DateIssued": now,
        "DateRedeemed": "",
        "RedeemedBy": "",
        "Source": source,
        "SubscribedSince": "",
        "Discount": DISCOUNT_LABEL,
    })
    return code, True

def redeem_code(code: str, staff_username: str) -> Tuple[bool, str]:
    for i, rec in enumerate(sheet.get_all_records(), start=2):
        if rec.get("PromoCode") == code:
            if rec.get("DateRedeemed"):
                return False, (
                    "❌ Код уже погашен ранее.\n"
                    f"Скидка: {rec.get('Discount', '')}\n"
                    f"Дата выдачи: {rec.get('DateIssued', '')}\n"
                    f"Дата погашения: {rec.get('DateRedeemed', '')}\n"
                    f"Погасил: {rec.get('RedeemedBy', '')}\n"
                )
            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            headers_now = sheet.row_values(1)
            idx = {h: headers_now.index(h) for h in headers_now if h in headers_now}
            sheet.update_cell(i, idx["DateRedeemed"] + 1, now)
            sheet.update_cell(i, idx["RedeemedBy"] + 1, staff_username or "Staff")
            discount = rec.get("Discount", DISCOUNT_LABEL)
            issued = rec.get("DateIssued", "")
            source = rec.get("Source", "")
            reply = (
                "✅ Код действителен и помечен как использованный.\n\n"
                f"Код: <b>{code}</b>\n"
                f"Скидка: <b>{discount}</b>\n"
                f"Выдан: {issued}\n"
                f"Источник: {source}\n"
                f"Сотрудник: @{staff_username if staff_username else 'Staff'}"
            )
            return True, reply
    return False, "Промокод не найден ❌"

def is_subscribed(user_id: int) -> bool:
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def do_check_subscription(chat_id: int, user):
    if not is_subscribed(user.id):
        bot.send_message(
            chat_id,
            f"Подпишись на {CHANNEL_USERNAME}, затем повтори проверку.",
            reply_markup=inline_subscribe_keyboard()
        )
        return
    if not can_issue(user.id):
        bot.send_message(chat_id, "Спасибо за подписку! Промокод станет доступен позже.")
        return
    src = USER_SOURCE.get(user.id, "subscribe")
    code, _ = issue_code(user.id, user.username, source=src)
    bot.send_message(
        chat_id,
        f"Спасибо за подписку на {CHANNEL_USERNAME}! 🎉\nТвой промокод: <b>{code}</b>",
        parse_mode="HTML"
    )

# ---------- Вспомогательные функции для статистики ----------
def parse_iso(dt_str: str) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def month_bounds(year: int, month: int) -> Tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    end = datetime(year, month, last_day, 23, 59, 59)
    return start, end

def get_subscribe_date(rec: dict) -> Optional[datetime]:
    return parse_iso(rec.get("SubscribedSince") or rec.get("DateIssued") or "")

def ensure_unsubscribed_col():
    ensure_column("UnsubscribedAt")

def refresh_unsubs(max_checks: Optional[int] = None) -> Tuple[int, int]:
    ensure_unsubscribed_col()
    hdrs = sheet.row_values(1)
    idx = {h: hdrs.index(h) for h in hdrs}
    updated = 0
    checked = 0
    records = sheet.get_all_records()
    for i, rec in enumerate(records, start=2):
        if max_checks is not None and checked >= max_checks:
            break
        uid = rec.get("UserID")
        if not uid:
            continue
        uid = int(str(uid))
        if rec.get("UnsubscribedAt"):
            continue
        if not get_subscribe_date(rec):
            continue
        checked += 1
        try:
            m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=uid)
            if m.status in ("left", "kicked"):
                now = datetime.now().isoformat(sep=" ", timespec="seconds")
                sheet.update_cell(i, idx["UnsubscribedAt"] + 1, now)
                updated += 1
        except Exception:
            pass
    return checked, updated

def aggregate_by_source(period: Optional[Tuple[datetime, datetime]] = None) -> Tuple[Dict[str, int], Dict[str, int]]:
    subs: Dict[str, int] = {}
    unsubs: Dict[str, int] = {}
    records = sheet.get_all_records()
    for rec in records:
        src = (rec.get("Source") or "default").strip() or "default"
        sub_dt = get_subscribe_date(rec)
        if sub_dt:
            if period is None or (period[0] <= sub_dt <= period[1]):
                subs[src] = subs.get(src, 0) + 1
        unsub_dt = parse_iso(rec.get("UnsubscribedAt") or "")
        if unsub_dt:
            if period is None or (period[0] <= unsub_dt <= period[1]):
                unsubs[src] = unsubs.get(src, 0) + 1
    return subs, unsubs

def format_stats_by_source(title: str, subs: Dict[str, int], unsubs: Dict[str, int]) -> str:
    all_sources = sorted(set(list(subs.keys()) + list(unsubs.keys())))
    total_sub = sum(subs.get(s, 0) for s in all_sources)
    total_unsub = sum(unsubs.get(s, 0) for s in all_sources)
    lines = [f"📊 {title}"]
    if not all_sources:
        lines.append("Нет данных.")
        return "\n".join(lines)
    for s in all_sources:
        a = subs.get(s, 0)
        b = unsubs.get(s, 0)
        lines.append(f"{s:10s} — подписки: {a} / отписки: {b} / прирост: {a - b:+d}")
    lines.append("")
    lines.append(f"Итого: подписки {total_sub}, отписки {total_unsub}, прирост {total_sub - total_unsub:+d}")
    return "\n".join(lines)

# ---------- Handlers: старт/о бренде/инлайн промокод ----------
@bot.message_handler(commands=["start", "help"])
def start(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        USER_SOURCE[message.from_user.id] = parts[1].strip()[:32].lower()
    bot.send_message(message.chat.id, WELCOME, reply_markup=make_main_keyboard(message.from_user.id))
    bot.send_message(message.chat.id, "Хочешь промокод? Нажми кнопку ниже 👇", reply_markup=inline_subscribe_keyboard())

@bot.callback_query_handler(func=lambda c: c.data == "check_and_issue")
def cb_check_and_issue(cb):
    do_check_subscription(cb.message.chat.id, cb.from_user)
    try:
        bot.answer_callback_query(cb.id)
    except Exception:
        pass

@bot.message_handler(func=lambda m: m.text == BTN_ABOUT)
def handle_about(message):
    bot.reply_to(message, BRAND_ABOUT, parse_mode="HTML")

# ---------- Статистика (админ) ----------
@bot.message_handler(commands=["subs_all"])
def cmd_subs_all(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    subs, unsubs = aggregate_by_source(period=None)
    text = format_stats_by_source("Подписки по источникам — все время", subs, unsubs)
    bot.reply_to(message, text)

@bot.message_handler(commands=["subs_month"])
def cmd_subs_month(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    parts = message.text.split(maxsplit=1)
    now = datetime.now()
    if len(parts) > 1:
        arg = parts[1].strip()
        try:
            y, m = arg.split("-")
            year, month = int(y), int(m)
        except Exception:
            bot.reply_to(message, "Формат: /subs_month YYYY-MM (например, /subs_month 2025-08)")
            return
    else:
        year, month = now.year, now.month
    start, end = month_bounds(year, month)
    subs, unsubs = aggregate_by_source(period=(start, end))
    title = f"Подписки по источникам — {year}-{str(month).zfill(2)}"
    text = format_stats_by_source(title, subs, unsubs)
    bot.reply_to(message, text)

@bot.message_handler(commands=["subs_refresh"])
def cmd_subs_refresh(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    max_checks = None  # можно поставить число (например, 500), чтобы ограничить за раз
    checked, updated = refresh_unsubs(max_checks=max_checks)
    bot.reply_to(message, f"Проверено: {checked}, обновлено UnsubscribedAt: {updated}")

# --- Инлайн-меню статистики ---
CB_SUBS_MENU_CUR = "subs_menu_cur"
CB_SUBS_MENU_PREV = "subs_menu_prev"
CB_SUBS_MENU_ALL = "subs_menu_all"
CB_SUBS_MENU_PICK = "subs_menu_pick"

def send_subs_menu(chat_id: int):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(
        telebot.types.InlineKeyboardButton("🗓 Текущий месяц", callback_data=CB_SUBS_MENU_CUR),
        telebot.types.InlineKeyboardButton("⏮ Прошлый месяц", callback_data=CB_SUBS_MENU_PREV),
    )
    kb.add(
        telebot.types.InlineKeyboardButton("📆 Выбрать месяц", callback_data=CB_SUBS_MENU_PICK),
        telebot.types.InlineKeyboardButton("∞ Всё время", callback_data=CB_SUBS_MENU_ALL),
    )
    bot.send_message(chat_id, "Выберите период для статистики:", reply_markup=kb)

@bot.message_handler(commands=["subs_menu"])
def cmd_subs_menu(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    send_subs_menu(message.chat.id)

def send_month_stats(chat_id: int, year: int, month: int):
    start, end = month_bounds(year, month)
    subs, unsubs = aggregate_by_source(period=(start, end))
    title = f"Подписки по источникам — {year}-{str(month).zfill(2)}"
    text = format_stats_by_source(title, subs, unsubs)
    bot.send_message(chat_id, text)

def send_alltime_stats(chat_id: int):
    subs, unsubs = aggregate_by_source(period=None)
    text = format_stats_by_source("Подписки по источникам — все время", subs, unsubs)
    bot.send_message(chat_id, text)

@bot.callback_query_handler(func=lambda c: c.data in {CB_SUBS_MENU_CUR, CB_SUBS_MENU_PREV, CB_SUBS_MENU_ALL, CB_SUBS_MENU_PICK})
def cb_subs_menu(cb):
    uid = cb.from_user.id
    if not is_admin(uid):
        try: bot.answer_callback_query(cb.id, "Только для администратора.")
        except Exception: pass
        return

    now = datetime.now()
    if cb.data == CB_SUBS_MENU_CUR:
        send_month_stats(cb.message.chat.id, now.year, now.month)
    elif cb.data == CB_SUBS_MENU_PREV:
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        send_month_stats(cb.message.chat.id, prev_year, prev_month)
    elif cb.data == CB_SUBS_MENU_ALL:
        send_alltime_stats(cb.message.chat.id)
    elif cb.data == CB_SUBS_MENU_PICK:
        STATE[uid] = "await_month_pick"
        bot.send_message(cb.message.chat.id, "Введите месяц в формате <b>YYYY-MM</b>, например <code>2025-08</code>.", parse_mode="HTML")
    try:
        bot.answer_callback_query(cb.id)
    except Exception:
        pass

# ---------- Персонал / Админ (ВЫШЕ общего обработчика!) ----------
@bot.message_handler(func=lambda m: m.text == BTN_STAFF_VERIFY)
def handle_staff_verify(message):
    if not is_staff(message.from_user.id):
        bot.reply_to(message, "Доступно только сотрудникам.")
        return
    STATE[message.from_user.id] = "await_code"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(message, "Введите промокод для проверки/погашения (4 символа) или нажмите «Отмена».", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == BTN_ADMIN_ADD_STAFF)
def handle_admin_add_staff(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    STATE[message.from_user.id] = "await_staff_id"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(
        message,
        "Пришлите ID пользователя-сотрудника (цифрами), или перешлите его сообщение, "
        "или отправьте его контакт из Telegram. Либо «Отмена».",
        reply_markup=kb
    )

@bot.message_handler(content_types=["contact"])
def handle_contact(message):
    uid = message.from_user.id
    if STATE.get(uid) != "await_staff_id":
        return
    contact = message.contact
    if contact and getattr(contact, "user_id", None):
        add_staff_id(int(contact.user_id))
        STATE.pop(uid, None)
        bot.reply_to(message, f"Сотрудник добавлен: {contact.user_id} ✅", reply_markup=make_main_keyboard(uid))
    else:
        bot.reply_to(message, "Этот контакт не содержит user_id Telegram. Пришлите ID цифрами или перешлите сообщение.")

# ---------- Отзывы ----------
@bot.message_handler(func=lambda m: m.text == BTN_FEEDBACK)
def handle_feedback_start(message):
    uid = message.from_user.id
    FEEDBACK_DRAFT[uid] = {"rating": None, "text": None, "photos": []}
    STATE[uid] = "await_feedback_rating"
    bot.reply_to(message, "Оцените нас по пятибалльной шкале (1 – плохо, 5 – отлично).", reply_markup=rating_keyboard())

@bot.message_handler(func=lambda m: m.text in RATING_BTNS)
def handle_feedback_rating(message):
    uid = message.from_user.id
    if STATE.get(uid) != "await_feedback_rating":
        return
    rating = int((message.text or "").split()[-1])
    FEEDBACK_DRAFT[uid]["rating"] = rating
    STATE[uid] = "await_feedback_text"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(message, "Спасибо! Теперь напишите ваш отзыв одним сообщением.", reply_markup=kb)

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id
    if STATE.get(uid) != "await_feedback_photos":
        return
    file_id = message.photo[-1].file_id
    photos: List[str] = FEEDBACK_DRAFT[uid]["photos"]
    if len(photos) < 5:
        photos.append(file_id)
        bot.reply_to(message, f"Фото добавлено ({len(photos)}/5).", reply_markup=photos_keyboard())
    else:
        bot.reply_to(message, "Можно прикрепить не более 5 фото.", reply_markup=photos_keyboard())

@bot.message_handler(func=lambda m: m.text == BTN_SEND_FEEDBACK or m.text == BTN_SKIP_PHOTOS)
def handle_feedback_submit_buttons(message):
    uid = message.from_user.id
    if STATE.get(uid) != "await_feedback_photos":
        return
    draft = FEEDBACK_DRAFT.get(uid, {})
    feedback_ws.append_row([
        str(uid),
        message.from_user.username or "",
        str(draft.get("rating")),
        draft.get("text"),
        ",".join(draft.get("photos", [])),
        datetime.now().isoformat(sep=" ", timespec="seconds")
    ])
    STATE.pop(uid, None)
    FEEDBACK_DRAFT.pop(uid, None)
    bot.reply_to(message, "Спасибо за отзыв! Он сохранён ✅", reply_markup=make_main_keyboard(uid))

@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def handle_cancel(message):
    uid = message.from_user.id
    STATE.pop(uid, None)
    FEEDBACK_DRAFT.pop(uid, None)
    bot.reply_to(message, "Отменено.", reply_markup=make_main_keyboard(uid))

# ---------- Общий обработчик ТЕКСТА (последним!) ----------
@bot.message_handler(content_types=["text"])
def handle_text_general(message):
    uid = message.from_user.id
    state = STATE.get(uid)

    # Ввод месяца для статистики (после кнопки «📆 Выбрать месяц»)
    if state == "await_month_pick":
        txt = (message.text or "").strip()
        try:
            y, m = txt.split("-")
            year, month = int(y), int(m)
            STATE.pop(uid, None)
            send_month_stats(message.chat.id, year, month)
            return
        except Exception:
            bot.reply_to(message, "Неверный формат. Введите месяц как <b>YYYY-MM</b>, например <code>2025-08</code>.", parse_mode="HTML")
            return

    if state == "await_feedback_text":
        text = (message.text or "").strip()
        FEEDBACK_DRAFT[uid]["text"] = text
        STATE[uid] = "await_feedback_photos"
        bot.reply_to(
            message,
            "Отлично! Теперь можете прислать до 5 фото. Когда будете готовы — нажмите «✅ Отправить» или «⏩ Пропустить фото».",
            reply_markup=photos_keyboard()
        )
        return

    if state == "await_feedback_photos":
        bot.reply_to(message, "Пришлите фото или нажмите «✅ Отправить» / «⏩ Пропустить фото».", reply_markup=photos_keyboard())
        return

    if state == "await_staff_id":
        new_id = None
        if hasattr(message, "forward_from") and message.forward_from:
            new_id = message.forward_from.id
        else:
            txt = (message.text or "").strip()
            if txt.isdigit():
                new_id = int(txt)
        if not new_id:
            bot.reply_to(message, "Не удалось определить ID. Пришлите число, перешлите сообщение или отправьте контакт.")
            return
        add_staff_id(int(new_id))
        STATE.pop(uid, None)
        bot.reply_to(message, f"Сотрудник добавлен: {new_id} ✅", reply_markup=make_main_keyboard(uid))
        return

    if state == "await_code":
        code = (message.text or "").strip().upper()
        if len(code) != 4 or not all(ch in (string.ascii_uppercase + string.digits) for ch in code):
            bot.reply_to(message, "Неверный формат. Введите 4 символа A–Z/0–9.")
            return
        ok, info = redeem_code(code, message.from_user.username or "Staff")
        STATE.pop(uid, None)
        bot.reply_to(message, info, parse_mode="HTML", reply_markup=make_main_keyboard(uid))
        return

    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Используйте кнопки снизу 👇", reply_markup=make_main_keyboard(uid))
    else:
        bot.reply_to(message, "Выберите действие на клавиатуре ниже 👇", reply_markup=make_main_keyboard(uid))

# ---------- FLASK (WEBHOOK/POLLING) ----------
app = Flask(__name__)

_ext = os.getenv("BASE_URL", "").strip()
if not _ext:
    host = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
    if host:
        _ext = f"https://{host}"
BASE_URL = _ext
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}" if BASE_URL else ""

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def telegram_webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook error:", e)
    return "OK", 200

def run_with_webhook():
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message","callback_query"])
        print("Webhook set to:", WEBHOOK_URL)
        port = int(os.getenv("PORT", "10000"))
        print("SBALO Promo Bot (Webhook) started on port", port)
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        print("Failed to set webhook, switching to polling:", e)
        run_with_polling()

def run_with_polling():
    print("Starting bot in long polling mode...")
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.infinity_polling(none_stop=True, timeout=60, long_polling_timeout=60)

if __name__ == "__main__":
    if WEBHOOK_URL:
        run_with_webhook()
    else:
        print("BASE_URL is empty; falling back to polling.")
        run_with_polling()
