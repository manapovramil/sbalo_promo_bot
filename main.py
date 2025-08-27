# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Webhook версия для Render
Изменения:
- Новое приветствие
- Новое описание бренда
- Отзывы с рейтингом и фото (сохраняются в Google Sheets)
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

# Состояния
STATE: Dict[int, str] = {}  
USER_SOURCE: Dict[int, str] = {}  

FEEDBACK_DRAFT: Dict[int, Dict] = {}  

# Текст кнопок
BTN_ABOUT = "ℹ️ О бренде"
BTN_FEEDBACK = "📝 Оставить отзыв"
BTN_STAFF_VERIFY = "✅ Проверить/Погасить код"
BTN_ADMIN_ADD_STAFF = "➕ Добавить сотрудника"
BTN_CANCEL = "❌ Отмена"
BTN_SKIP_PHOTOS = "⏩ Пропустить фото"
BTN_SEND_FEEDBACK = "✅ Отправить"

RATING_BTNS = ["⭐ 1","⭐ 2","⭐ 3","⭐ 4","⭐ 5"]

def make_main_keyboard(user_id: int):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(telebot.types.KeyboardButton(BTN_ABOUT), telebot.types.KeyboardButton(BTN_FEEDBACK))
    if not STAFF_IDS or (user_id in STAFF_IDS):
        kb.add(telebot.types.KeyboardButton(BTN_STAFF_VERIFY))
    if ADMIN_ID and user_id == ADMIN_ID:
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
    ikb.add(telebot.types.InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    ikb.add(telebot.types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub"))
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

# ---------- Утилиты ----------
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

def is_subscribed(user_id):
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

# ---------- Handlers ----------
@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.send_message(
        message.chat.id,
        WELCOME,
        reply_markup=make_main_keyboard(message.from_user.id)
    )
    bot.send_message(
        message.chat.id,
        "Ссылка на канал и проверка подписки:",
        reply_markup=inline_subscribe_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == BTN_ABOUT)
def handle_about(message):
    bot.reply_to(message, BRAND_ABOUT, parse_mode="HTML")

# ---------- Отзывы (рейтинг + текст + фото) ----------
@bot.message_handler(func=lambda m: m.text == BTN_FEEDBACK)
def handle_feedback_start(message):
    uid = message.from_user.id
    FEEDBACK_DRAFT[uid] = {"rating": None, "text": None, "photos": []}
    STATE[uid] = "await_feedback_rating"
    bot.reply_to(
        message,
        "Оцените нас по пятибалльной шкале (1 – плохо, 5 – отлично).",
        reply_markup=rating_keyboard()
    )

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
    bot.reply_to(
        message,
        "Спасибо! Теперь напишите ваш отзыв одним сообщением.",
        reply_markup=kb
    )

@bot.message_handler(content_types=["text"])
def handle_text_general(message):
    uid = message.from_user.id
    state = STATE.get(uid)

    if state == "await_feedback_text":
        text = (message.text or "").strip()
        FEEDBACK_DRAFT[uid]["text"] = text
        STATE[uid] = "await_feedback_photos"
        bot.reply_to(
            message,
            "Отлично! Теперь можете прислать до 5 фото. "
            "Когда будете готовы — нажмите «✅ Отправить» или «⏩ Пропустить фото».",
            reply_markup=photos_keyboard()
        )
        return

    if state == "await_feedback_photos" and message.text in (BTN_SEND_FEEDBACK, BTN_SKIP_PHOTOS):
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
        return

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id
    if STATE.get(uid) != "await_feedback_photos":
        return
    file_id = message.photo[-1].file_id
    if len(FEEDBACK_DRAFT[uid]["photos"]) < 5:
        FEEDBACK_DRAFT[uid]["photos"].append(file_id)
        bot.reply_to(message, f"Фото добавлено ({len(FEEDBACK_DRAFT[uid]['photos'])}/5).", reply_markup=photos_keyboard())
    else:
        bot.reply_to(message, "Можно прикрепить не более 5 фото.", reply_markup=photos_keyboard())

# ---------- FLASK (WEBHOOK) ----------
app = Flask(__name__)
BASE_URL = os.getenv("BASE_URL") or f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME','')}".rstrip("/")
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"

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

if __name__ == "__main__":
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message","callback_query"])
        print("Webhook set to:", WEBHOOK_URL)
    except Exception as e:
        print("Failed to set webhook:", e)

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
