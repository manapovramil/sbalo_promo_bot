# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Webhook версия для Render
Изменения:
- Приветствие (WELCOME) и описание бренда (BRAND_ABOUT)
- Inline-кнопка: «🎁 Проверить подписку и получить промокод»
- Администратор теперь тоже может проверять/гасить коды (считается staff)
- Исправлено добавление сотрудника: ID цифрами, пересланное сообщение, контакт
- Отзывы: рейтинг + текст + фото (лист Feedback)
"""

import os, random, string
from datetime import datetime
from typing import Dict, Set, List

import telebot
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
STAFF_IDS: Set[int] = set(int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip().isdigit())
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUBSCRIPTION_MIN_DAYS = int(os.getenv("SUBSCRIPTION_MIN_DAYS", "0"))
SERVICE_ACCOUNT_JSON_ENV = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()
DISCOUNT_LABEL = os.getenv("DISCOUNT_LABEL", "7%")

if not SERVICE_ACCOUNT_JSON_ENV:
    raise SystemExit("ENV SERVICE_ACCOUNT_JSON пуст — вставьте содержимое credentials.json в переменную окружения.")
missing = [k for k, v in [("BOT_TOKEN", BOT_TOKEN),
                          ("CHANNEL_USERNAME", CHANNEL_USERNAME),
                          ("SPREADSHEET_ID", SPREADSHEET_ID)] if not v]
if missing:
    raise SystemExit("Нет переменных окружения: " + ", ".join(missing))

# ---------- Google Sheets ----------
CREDENTIALS_PATH = "/tmp/credentials.json"
with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
    f.write(SERVICE_ACCOUNT_JSON_ENV)

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES)
client = gspread.authorize(creds)

# Основной лист (промокоды/подписка)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1
HEADERS = ["UserID","Username","PromoCode","DateIssued","DateRedeemed","RedeemedBy","OrderID","Source","SubscribedSince","Discount"]
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

# Состояния и черновики
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

# ---------- Промо/подписка ----------
def generate_short_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=4))
        if any(ch.isalpha() for ch in code):
            return code

def append_row_dict(ws, header_list, data: dict):
    headers_now = ws.row_values(1)
    if not headers_now:
        ws.append_row(header_list)
        headers_now = header_list[:]
    row = [""] * len(headers_now)
    for k, v in data.items():
        if k in headers_now:
            row[headers_now.index(k)] = str(v)
    ws.append_row(row)

def get_row_by_user(user_id):
    for i, rec in enumerate(sheet.get_all_records(), start=2):
        if str(rec.get("UserID")) == str(user_id):
            return i, rec
    return None, None

def find_user_code(user_id):
    i, rec = get_row_by_user(user_id)
    if i and rec.get("PromoCode"):
        return i, rec["PromoCode"]
    return None, None

def ensure_subscribed_since(user_id):
    i, rec = get_row_by_user(user_id)
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    headers_now = sheet.row_values(1)
    if "SubscribedSince" not in headers_now:
        sheet.update_cell(1, len(headers_now) + 1, "SubscribedSince")
        headers_now = sheet.row_values(1)
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

def can_issue(user_id):
    if SUBSCRIPTION_MIN_DAYS <= 0:
        return True
    since = ensure_subscribed_since(user_id)
    return (datetime.now() - since).days >= SUBSCRIPTION_MIN_DAYS

def issue_code(user_id, username, source="subscribe"):
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

def redeem_code(code, staff_username):
    for i, rec in enumerate(sheet.get_all_records(), start=2):
        if rec.get("PromoCode") == code:
            if rec.get("DateRedeemed"):
                return False, "❌ Код уже погашен ранее."
            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            headers_now = sheet.row_values(1)
            idx = {h: headers_now.index(h) for h in headers_now if h in headers_now}
            sheet.update_cell(i, idx["DateRedeemed"] + 1, now)
            sheet.update_cell(i, idx["RedeemedBy"] + 1, staff_username or "Staff")
            discount = rec.get("Discount", DISCOUNT_LABEL)
            reply = f"✅ Код {code} действителен. Скидка: {discount}"
            return True, reply
    return False, "Промокод не найден ❌"

def is_subscribed(user_id):
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def do_check_subscription(chat_id, user):
    if not is_subscribed(user.id):
        bot.send_message(chat_id, f"Подпишись на {CHANNEL_USERNAME}, затем повтори проверку.", reply_markup=inline_subscribe_keyboard())
        return
    if not can_issue(user.id):
        bot.send_message(chat_id, "Спасибо за подписку! Промокод станет доступен позже.")
        return
    src = USER_SOURCE.get(user.id, "subscribe")
    code, _ = issue_code(user.id, user.username, source=src)
    bot.send_message(chat_id, f"Спасибо за подписку на {CHANNEL_USERNAME}! 🎉\nТвой промокод: <b>{code}</b>", parse_mode="HTML")

# ---------- Handlers ----------
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

# ... (остальная часть обработчиков остаётся без изменений: отзывы, staff, admin, webhook)
