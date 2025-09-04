# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Render (webhook с fallback на polling)

Ключевые функции:
- Главные кнопки: «О бренде», «Оставить отзыв», для сотрудников — «Проверить/Погасить код», «Статистика», «Добавить сотрудника»
- Статистика подписок/отписок по источникам (месяц/всё время)
- Надёжная запись в Google Sheets (lock + retries)
- Промокод 1 на пользователя (upsert в строку)
- Автовыдача промокода после фактической подписки (без сообщений пользователю)
- Фиксация источника из /start-параметра в Source сразу при клике «Подписаться»
"""

import os, random, string, calendar, threading
from threading import Timer
from time import sleep
from datetime import datetime
from typing import Dict, Set, List, Tuple, Optional

import telebot
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")  # например: @sbalo_channel
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")

# STAFF_IDS — сотрудники (могут гасить коды, смотреть статистику, добавлять сотрудников)
STAFF_IDS: Set[int] = set(int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip().isdigit())

# ADMIN_IDS — админы (полный доступ, включая /subs_refresh)
ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

SUBSCRIPTION_MIN_DAYS = int(os.getenv("SUBSCRIPTION_MIN_DAYS", "0"))
SERVICE_ACCOUNT_JSON = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
DISCOUNT_LABEL = os.getenv("DISCOUNT_LABEL", "5%")  # скидка по умолчанию

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

# Глобальный lock для всех операций с таблицей
GS_LOCK = threading.Lock()

# Универсальные безопасные обёртки с ретраями
def _with_retries(fn, *args, retries=3, backoff=0.7, **kwargs):
    last_err = None
    for i in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if i == retries - 1:
                raise
            sleep(backoff * (2 ** i))
    if last_err:
        raise last_err

def gs_append_row_safe(ws, row: list):
    with GS_LOCK:
        return _with_retries(ws.append_row, row)

def gs_update_cell_safe(ws, r: int, c: int, value: str):
    with GS_LOCK:
        return _with_retries(ws.update_cell, r, c, value)

def gs_get_all_records_safe(ws):
    with GS_LOCK:
        return _with_retries(ws.get_all_records)

def gs_find_safe(ws, query: str):
    with GS_LOCK:
        return _with_retries(ws.find, query)

def gs_row_values_safe(ws, row: int):
    with GS_LOCK:
        return _with_retries(ws.row_values, row)

def get_col_map(ws) -> dict:
    hdrs = gs_row_values_safe(ws, 1)
    return {h: i + 1 for i, h in enumerate(hdrs)}

def update_row_fields(ws, row_idx: int, fields: dict):
    colmap = get_col_map(ws)
    for k, v in fields.items():
        if k in colmap:
            gs_update_cell_safe(ws, row_idx, colmap[k], "" if v is None else str(v))

# Основной лист
sheet = client.open_by_key(SPREADSHEET_ID).sheet1
HEADERS = [
    "UserID","Username","PromoCode","DateIssued","DateRedeemed","RedeemedBy",
    "OrderID","Source","SubscribedSince","Discount","UnsubscribedAt",
    "SubscribeClickedAt","AutoIssuedAt"
]
headers = gs_row_values_safe(sheet, 1)
if not headers:
    gs_append_row_safe(sheet, HEADERS)
    headers = HEADERS[:]
else:
    for h in HEADERS:
        if h not in headers:
            gs_update_cell_safe(sheet, 1, len(headers) + 1, h)
            headers.append(h)

# Лист отзывов
try:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Feedback")
except gspread.WorksheetNotFound:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).add_worksheet(title="Feedback", rows=2000, cols=6)
    gs_append_row_safe(feedback_ws, ["UserID","Username","Rating","Text","Photos","Date"])

# ---------- Telegram ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

STATE: Dict[int, str] = {}
USER_SOURCE: Dict[int, str] = {}   # фиксируем utm/источник из /start
FEEDBACK_DRAFT: Dict[int, Dict] = {}

# Для авто-проверок членства после нажатия «Подписаться»
PENDING_SUB: Dict[int, Dict] = {}

# ---------- Кнопки ----------
BTN_ABOUT = "ℹ️ О бренде"
BTN_FEEDBACK = "📝 Оставить отзыв"
BTN_STAFF_VERIFY = "✅ Проверить/Погасить код"
BTN_ADMIN_ADD_STAFF = "➕ Добавить сотрудника"  # видна сотрудникам и админам
BTN_STATS_MENU = "📊 Статистика"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP_PHOTOS = "⏩ Пропустить фото"
BTN_SEND_FEEDBACK = "✅ Отправить"
RATING_BTNS = ["⭐ 1","⭐ 2","⭐ 3","⭐ 4","⭐ 5"]

# ---------- Права ----------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

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
        kb.add(telebot.types.KeyboardButton(BTN_STATS_MENU))
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
    ikb.add(telebot.types.InlineKeyboardButton("✅ Подписаться на канал", callback_data="want_subscribe"))
    ikb.add(telebot.types.InlineKeyboardButton("🎁Получить промокод", callback_data="check_and_issue"))
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
    headers_now = gs_row_values_safe(ws, 1)
    if not headers_now:
        gs_append_row_safe(ws, header_list)
        headers_now = header_list[:]
    row = [""] * len(headers_now)
    for k, v in data.items():
        if k in headers_now:
            row[headers_now.index(k)] = str(v)
    gs_append_row_safe(ws, row)

def get_row_by_user(user_id: int) -> Tuple[Optional[int], Optional[dict]]:
    records = gs_get_all_records_safe(sheet)
    for i, rec in enumerate(records, start=2):
        if str(rec.get("UserID")) == str(user_id):
            return i, rec
    return None, None

def find_user_code(user_id: int) -> Tuple[Optional[int], Optional[str]]:
    i, rec = get_row_by_user(user_id)
    if i and rec.get("PromoCode"):
        return i, rec["PromoCode"]
    return None, None

def ensure_column(name: str):
    hdrs = gs_row_values_safe(sheet, 1)
    if name not in hdrs:
        gs_update_cell_safe(sheet, 1, len(hdrs) + 1, name)

# ---------- Промо/подписка ----------
def generate_short_code() -> str:
    # 4 символа A–Z/0–9, минимум одна буква
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
        col = gs_row_values_safe(sheet, 1).index("SubscribedSince") + 1
        gs_update_cell_safe(sheet, i, col, now)
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
    """
    UPsert: гарантируем 1 код и 1 строку на пользователя.
    - Если код уже есть — возвращаем его (created=False).
    - Если строки нет — создаём новую.
    - Если строка есть — обновляем её полями PromoCode/DateIssued/Discount.
      Source заполняем только если пуст (чтобы не перетирать UTM из /start).
    Возвращает: (code, created_bool)
    """
    row_idx, rec = get_row_by_user(user_id)
    if row_idx and rec and rec.get("PromoCode"):
        return rec["PromoCode"], False

    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    code = generate_short_code()

    if row_idx:
        fields = {
            "Username": username or rec.get("Username") or "",
            "PromoCode": code,
            "DateIssued": now,
            "Discount": DISCOUNT_LABEL,
        }
        if not rec.get("Source"):
            fields["Source"] = source
        if source == "auto_issue" and not rec.get("AutoIssuedAt"):
            fields["AutoIssuedAt"] = now
        update_row_fields(sheet, row_idx, fields)
    else:
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
            "AutoIssuedAt": now if source == "auto_issue" else "",
        })

    # Верифицируем по user_id (независимо от append/update)
    row_idx2, rec2 = get_row_by_user(user_id)
    if not row_idx2 or not rec2 or not rec2.get("PromoCode"):
        raise RuntimeError("Code not persisted for user")

    return rec2["PromoCode"], True

def redeem_code(code: str, staff_username: str) -> Tuple[bool, str]:
    try:
        cell = gs_find_safe(sheet, code)
    except Exception:
        return False, "Промокод не найден ❌"

    if not cell:
        return False, "Промокод не найден ❌"

    row_idx = cell.row
    headers_now = gs_row_values_safe(sheet, 1)
    recs = gs_get_all_records_safe(sheet)
    rec = recs[row_idx - 2] if row_idx >= 2 and (row_idx - 2) < len(recs) else {}

    if rec.get("DateRedeemed"):
        return False, (
            "❌ Код уже погашен ранее.\n"
            f"Скидка: {rec.get('Discount', '')}\n"
            f"Дата выдачи: {rec.get('DateIssued', '')}\n"
            f"Дата погашения: {rec.get('DateRedeemed', '')}\n"
            f"Погасил: {rec.get('RedeemedBy', '')}\n"
        )

    idx = {h: headers_now.index(h) for h in headers_now if h in headers_now}
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    gs_update_cell_safe(sheet, row_idx, idx["DateRedeemed"] + 1, now)
    gs_update_cell_safe(sheet, row_idx, idx["RedeemedBy"] + 1, staff_username or "Staff")

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

def is_subscribed(user_id: int) -> bool:
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ---------- Логика «Подписаться» с источником и авто-выдачей кода ----------
def mark_subscribe_click(user_id: int, username: str):
    ensure_column("SubscribeClickedAt")
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    src = USER_SOURCE.get(user_id, "direct")

    i, rec = get_row_by_user(user_id)
    if i:
        hdrs = gs_row_values_safe(sheet, 1)
        col = hdrs.index("SubscribeClickedAt") + 1
        gs_update_cell_safe(sheet, i, col, now)
        if not rec.get("Source"):  # не перетираем уже заданный источник
            col_src = hdrs.index("Source") + 1
            gs_update_cell_safe(sheet, i, col_src, src)
    else:
        append_row_dict(sheet, HEADERS, {
            "UserID": str(user_id),
            "Username": username or "",
            "Source": src,
            "SubscribeClickedAt": now
        })

def schedule_membership_checks(user_id: int, chat_id: int):
    """
    Проверки членства: 20s, 2min, 10min.
    При первом подтверждении подписки:
      - SubscribedSince (если пусто)
      - issue_code(..., source="auto_issue") — апдейт в ту же строку
      - пользователю ничего не пишем
    """
    delays = [20, 120, 600]
    PENDING_SUB[user_id] = {"chat_id": chat_id, "t0": datetime.now()}

    def _check():
        _, rec = get_row_by_user(user_id)
        if rec and rec.get("PromoCode"):
            PENDING_SUB.pop(user_id, None)
            return

        if is_subscribed(user_id):
            ensure_subscribed_since(user_id)
            try:
                issue_code(user_id, "", source="auto_issue")
            except Exception as e:
                for admin_id in ADMIN_IDS:
                    try: bot.send_message(admin_id, f"⚠️ Auto-issue fail для {user_id}: {e}")
                    except: pass
            PENDING_SUB.pop(user_id, None)

    for d in delays:
        Timer(d, _check).start()

# ---------- Старт/кнопки ----------
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

@bot.callback_query_handler(func=lambda c: c.data == "want_subscribe")
def cb_want_subscribe(cb):
    u = cb.from_user
    chat_id = cb.message.chat.id

    if u.id not in USER_SOURCE:
        USER_SOURCE[u.id] = "direct"

    try:
        mark_subscribe_click(u.id, u.username or "")
    except Exception as e:
        print("mark_subscribe_click error:", e)

    try:
        bot.send_message(
            chat_id,
            f"Открой наш канал и подпишись: https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
        )
    except Exception:
        pass

    try:
        schedule_membership_checks(u.id, chat_id)
    except Exception as e:
        print("schedule_membership_checks error:", e)

    try:
        bot.answer_callback_query(cb.id)
    except Exception:
        pass

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
    try:
        code, _ = issue_code(user.id, user.username, source=src)
        bot.send_message(
            chat_id,
            f"Спасибо за подписку на {CHANNEL_USERNAME}! 🎉\nТвой промокод: <b>{code}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        alert = f"⚠️ Не удалось записать промокод в таблицу для user {user.id} (@{user.username}). Ошибка: {e}"
        for admin_id in ADMIN_IDS:
            try: bot.send_message(admin_id, alert)
            except Exception: pass
        bot.send_message(chat_id, "Сервис временно недоступен. Попробуйте ещё раз чуть позже 🙏")

@bot.message_handler(func=lambda m: m.text == BTN_ABOUT)
def handle_about(message):
    bot.reply_to(message, BRAND_ABOUT, parse_mode="HTML")

# ---------- Статистика ----------
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
    """Проставляет UnsubscribedAt тем, кто вышел из канала. Команда /subs_refresh (только админ)."""
    ensure_unsubscribed_col()
    hdrs = gs_row_values_safe(sheet, 1)
    idx = {h: hdrs.index(h) for h in hdrs}
    updated = 0
    checked = 0
    records = gs_get_all_records_safe(sheet)
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
                gs_update_cell_safe(sheet, i, idx["UnsubscribedAt"] + 1, now)
                updated += 1
        except Exception:
            pass
    return checked, updated

def aggregate_by_source(period: Optional[Tuple[datetime, datetime]] = None) -> Tuple[Dict[str, int], Dict[str, int]]:
    subs: Dict[str, int] = {}
    unsubs: Dict[str, int] = {}
    records = gs_get_all_records_safe(sheet)
    for rec in records:
        src = (rec.get("Source") or "default").strip() or "default"
        sub_dt = get_subscribe_date(rec)
        if sub_dt and (period is None or (period[0] <= sub_dt <= period[1])):
            subs[src] = subs.get(src, 0) + 1
        unsub_dt = parse_iso(rec.get("UnsubscribedAt") or "")
        if unsub_dt and (period is None or (period[0] <= unsub_dt <= period[1])):
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

# ---------- Инлайн-меню статистики ----------
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

# ---------- Статистика (меню/команды) ----------
@bot.message_handler(func=lambda m: m.text == BTN_STATS_MENU)
def handle_stats_menu_button(message):
    if not is_staff(message.from_user.id):
        bot.reply_to(message, "Доступно только сотрудникам.")
        return
    send_subs_menu(message.chat.id)

@bot.message_handler(commands=["subs_all"])
def cmd_subs_all(message):
    if not is_staff(message.from_user.id):
        bot.reply_to(message, "Доступно только сотрудникам.")
        return
    subs, unsubs = aggregate_by_source(period=None)
    text = format_stats_by_source("Подписки по источникам — все время", subs, unsubs)
    bot.reply_to(message, text)

@bot.message_handler(commands=["subs_month"])
def cmd_subs_month(message):
    if not is_staff(message.from_user.id):
        bot.reply_to(message, "Доступно только сотрудникам.")
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
    start_dt, end_dt = month_bounds(year, month)
    subs, unsubs = aggregate_by_source(period=(start_dt, end_dt))
    text = format_stats_by_source(f"Подписки по источникам — {year}-{str(month).zfill(2)}", subs, unsubs)
    bot.reply_to(message, text)

@bot.message_handler(commands=["subs_refresh"])
def cmd_subs_refresh(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "Доступно только администратору.")
        return
    max_checks = None
    checked, updated = refresh_unsubs(max_checks=max_checks)
    bot.reply_to(message, f"Проверено: {checked}, обновлено UnsubscribedAt: {updated}")

@bot.callback_query_handler(func=lambda c: c.data in {CB_SUBS_MENU_CUR, CB_SUBS_MENU_PREV, CB_SUBS_MENU_ALL, CB_SUBS_MENU_PICK})
def cb_subs_menu(cb):
    uid = cb.from_user.id
    if not is_staff(uid):
        try: bot.answer_callback_query(cb.id, "Только для сотрудников.")
        except Exception: pass
        return

    now = datetime.now()
    if cb.data == CB_SUBS_MENU_CUR:
        start_dt, end_dt = month_bounds(now.year, now.month)
        subs, unsubs = aggregate_by_source(period=(start_dt, end_dt))
        text = format_stats_by_source(f"Подписки по источникам — {now.year}-{str(now.month).zfill(2)}", subs, unsubs)
        bot.send_message(cb.message.chat.id, text)
    elif cb.data == CB_SUBS_MENU_PREV:
        prev_month = now.month - 1 or 12
        prev_year = now.year if now.month > 1 else now.year - 1
        start_dt, end_dt = month_bounds(prev_year, prev_month)
        subs, unsubs = aggregate_by_source(period=(start_dt, end_dt))
        text = format_stats_by_source(f"Подписки по источникам — {prev_year}-{str(prev_month).zfill(2)}", subs, unsubs)
        bot.send_message(cb.message.chat.id, text)
    elif cb.data == CB_SUBS_MENU_ALL:
        subs, unsubs = aggregate_by_source(period=None)
        text = format_stats_by_source("Подписки по источникам — все время", subs, unsubs)
        bot.send_message(cb.message.chat.id, text)
    elif cb.data == CB_SUBS_MENU_PICK:
        STATE[uid] = "await_month_pick"
        bot.send_message(cb.message.chat.id, "Введите месяц в формате <b>YYYY-MM</b>, например <code>2025-08</code>.", parse_mode="HTML")
    try:
        bot.answer_callback_query(cb.id)
    except Exception:
        pass

# ---------- Персонал / Админ ----------
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
    if not is_staff(message.from_user.id):
        bot.reply_to(message, "Доступно только сотрудникам.")
        return
    STATE[message.from_user.id] = "await_staff_id"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(
        message,
        "Пришлите ID пользователя-сотрудника (цифрами), перешлите его сообщение или отправьте его контакт. Либо «Отмена».",
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
        bot.reply_to(message, "Контакт не содержит user_id Telegram. Пришлите ID цифрами или перешлите сообщение.")

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
    gs_append_row_safe(feedback_ws, [
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

# ---------- Общий обработчик ТЕКСТА ----------
@bot.message_handler(content_types=["text"])
def handle_text_general(message):
    uid = message.from_user.id
    state = STATE.get(uid)

    if state == "await_month_pick":
        txt = (message.text or "").strip()
        try:
            y, m = txt.split("-")
            year, month = int(y), int(m)
            STATE.pop(uid, None)
            start_dt, end_dt = month_bounds(year, month)
            subs, unsubs = aggregate_by_source(period=(start_dt, end_dt))
            text = format_stats_by_source(f"Подписки по источникам — {year}-{str(month).zfill(2)}", subs, unsubs)
            bot.reply_to(message, text)
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
