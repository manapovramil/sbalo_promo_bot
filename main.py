# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Webhook версия для Render (бесплатный Web Service)
- Читает настройки из ENV (никаких config.json)
- Проверяет подписку на канал перед выдачей кода
- Выдаёт персональный промокод и пишет в Google Sheets
- Погашение кода сотрудником: /redeem CODE [ORDER_ID]
- Работает через Flask вебхук (а не polling) → подходит под бесплатный Render Web Service
"""

import os, random, string
from datetime import datetime, timedelta

import telebot
from telebot import apihelper
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- Настройки из ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
STAFF_IDS = set(int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip().isdigit())
SUBSCRIPTION_MIN_DAYS = int(os.getenv("SUBSCRIPTION_MIN_DAYS", "0"))
SERVICE_ACCOUNT_JSON_ENV = os.getenv("SERVICE_ACCOUNT_JSON", "").strip()

if not SERVICE_ACCOUNT_JSON_ENV:
    raise SystemExit("ENV SERVICE_ACCOUNT_JSON пуст — вставьте содержимое credentials.json в переменную окружения.")
missing = [k for k, v in [("BOT_TOKEN", BOT_TOKEN),
                          ("CHANNEL_USERNAME", CHANNEL_USERNAME),
                          ("SPREADSHEET_ID", SPREADSHEET_ID)] if not v]
if missing:
    raise SystemExit("Нет переменных окружения: " + ", ".join(missing))

# ---------- Google Sheets подключение ----------
CREDENTIALS_PATH = "/tmp/credentials.json"
with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
    f.write(SERVICE_ACCOUNT_JSON_ENV)

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

HEADERS = ["UserID","Username","PromoCode","DateIssued","DateRedeemed","RedeemedBy","OrderID","Source","SubscribedSince"]
first = sheet.row_values(1)
if first[:len(HEADERS)] != HEADERS:
    sheet.clear()
    sheet.append_row(HEADERS)

# ---------- Telegram Bot ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def generate_code(n=8):
    return "SBALO-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=n))

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
    headers = sheet.row_values(1)
    if "SubscribedSince" not in headers:
        sheet.clear(); sheet.append_row(HEADERS)
    if i and rec.get("SubscribedSince"):
        try:
            return datetime.fromisoformat(rec["SubscribedSince"])
        except:
            pass
    if i:
        col = sheet.row_values(1).index("SubscribedSince") + 1
        sheet.update_cell(i, col, now)
    else:
        sheet.append_row([str(user_id), "", "", "", "", "", "", "subscribe_check", now])
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
    code = generate_code()
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    sheet.append_row([str(user_id), username or "", code, now, "", "", "", source, ""])
    return code, True

def redeem_code(code, staff_username, order_id=""):
    for i, rec in enumerate(sheet.get_all_records(), start=2):
        if rec.get("PromoCode") == code:
            if rec.get("DateRedeemed"):
                return "Этот промокод уже был погашен ранее."
            now = datetime.now().isoformat(sep=" ", timespec="seconds")
            sheet.update_cell(i, 5, now)   # DateRedeemed
            sheet.update_cell(i, 6, staff_username or "Staff")  # RedeemedBy
            if order_id:
                sheet.update_cell(i, 7, order_id)  # OrderID
            return "Код успешно погашен ✅"
    return "Промокод не найден ❌"

def is_subscribed(user_id):
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except:
        return False

WELCOME = ("Привет! 👋 Это промо-бот <b>SBALO</b>.\n\n"
           "Подпишись на наш канал: {channel}\n"
           "После подписки нажми «Проверить подписку».")
           
@bot.message_handler(commands=["start","help"])
def start(message):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    kb.add(telebot.types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub"))
    bot.reply_to(message, WELCOME.format(channel=CHANNEL_USERNAME), reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(cb):
    u = cb.from_user
    if not is_subscribed(u.id):
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
        kb.add(telebot.types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub"))
        bot.answer_callback_query(cb.id, "Вы ещё не подписаны.")
        bot.edit_message_text(cb.message.text, cb.message.chat.id, cb.message.message_id, reply_markup=kb)
        return
    if not can_issue(u.id):
        bot.answer_callback_query(cb.id, "Недостаточный стаж подписки.")
        bot.edit_message_text("Спасибо за подписку! Промокод станет доступен позже.", cb.message.chat.id, cb.message.message_id)
        return
    code, _ = issue_code(u.id, u.username, source="subscribe")
    bot.edit_message_text(f"Спасибо за подписку на {CHANNEL_USERNAME}! 🎉\nТвой промокод: <b>{code}</b>",
                          cb.message.chat.id, cb.message.message_id, parse_mode="HTML")

@bot.message_handler(commands=["promo"])
def promo(message):
    u = message.from_user
    if not is_subscribed(u.id):
        kb = telebot.types.InlineKeyboardMarkup()
        kb.add(telebot.types.InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
        kb.add(telebot.types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub"))
        bot.reply_to(message, f"Подпишись на {CHANNEL_USERNAME}, затем нажми «Проверить подписку».", reply_markup=kb)
        return
    if not can_issue(u.id):
        bot.reply_to(message, "Спасибо за подписку! Промокод станет доступен позже.")
        return
    code, _ = issue_code(u.id, u.username, source="promo_cmd")
    bot.reply_to(message, f"Твой персональный промокод: <b>{code}</b> 🎁", parse_mode="HTML")

@bot.message_handler(commands=["redeem"])
def redeem(message):
    if STAFF_IDS and message.from_user.id not in STAFF_IDS:
        bot.reply_to(message, "Команда доступна только сотрудникам.")
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "Формат: /redeem КОД [НОМЕР_ЗАКАЗА]")
        return
    res = redeem_code(parts[1].strip(), message.from_user.username or "Staff",
                      parts[2].strip() if len(parts) > 2 else "")
    bot.reply_to(message, res)

# ---------- FLASK (WEBHOOK) ----------
app = Flask(__name__)

# Render подставляет внешний хост в RENDER_EXTERNAL_HOSTNAME
# Можно переопределить через BASE_URL, если нужно (например, свой домен)
BASE_URL = os.getenv("BASE_URL") or f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME','')}".rstrip("/")
WEBHOOK_PATH = f"/{BOT_TOKEN}"             # секретный путь
WEBHOOK_URL = f"{BASE_URL}{WEBHOOK_PATH}"  # полный адрес вебхука

@app.route("/", methods=["GET"])
def health():
    return "OK", 200

@app.before_first_request
def set_webhook():
    try:
        bot.remove_webhook()
        # Небольшая задержка иногда полезна; но Render стартует быстро — обойдёмся без sleep
        bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message","callback_query"])
        print("Webhook set to:", WEBHOOK_URL)
    except Exception as e:
        print("Failed to set webhook:", e)

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
    # Для локального запуска: python main.py (и потом /setWebhook не нужен)
    port = int(os.getenv("PORT", "10000"))
    print("SBALO Promo Bot (Webhook) started on port", port)
    print("Expecting Telegram updates at:", WEBHOOK_URL)
if __name__ == "__main__":
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL, allowed_updates=["message","callback_query"])
        print("Webhook set to:", WEBHOOK_URL)
    except Exception as e:
        print("Failed to set webhook:", e)

    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)

    app.run(host="0.0.0.0", port=port)
