# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Render-версия
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

# ... (весь блок функций issue_code, redeem_code и обработчики остаются без изменений) ...

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
