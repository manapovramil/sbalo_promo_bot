# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Webhook версия для Render (кнопки + короткие промокоды)
Изменения в этой версии:
- НИЖНЯЯ КЛАВИАТУРА: удалены «Получить промокод», «Проверить подписку»
- добавлены «ℹ️ О бренде», «📝 Оставить отзыв»
- Отзывы сохраняются в отдельный лист Google Sheets: Feedback
- Сохранил кнопки персонала/админа (проверка кода, добавление сотрудника)
"""

import os, random, string
from datetime import datetime
from typing import Dict, Set

import telebot
from flask import Flask, request

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---------- ENV ----------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
STAFF_IDS: Set[int] = set(int(x) for x in os.getenv("STAFF_IDS", "").split(",") if x.strip().isdigit())
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # владелец/админ бота
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

# ---------- Google Sheets подключение ----------
CREDENTIALS_PATH = "/tmp/credentials.json"
with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
    f.write(SERVICE_ACCOUNT_JSON_ENV)

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_PATH, SCOPES)
client = gspread.authorize(creds)
sheet = client.open_by_key(SPREADSHEET_ID).sheet1

# Основной лист с промокодами/подпиской
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

# Отдельный лист для отзывов
try:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).worksheet("Feedback")
except gspread.WorksheetNotFound:
    feedback_ws = client.open_by_key(SPREADSHEET_ID).add_worksheet(title="Feedback", rows=1000, cols=6)
    feedback_ws.append_row(["UserID","Username","Text","Date"])

# ---------- Telegram ----------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Глобальные состояния
STATE: Dict[int, str] = {}           # user_id -> state flag
USER_SOURCE: Dict[int, str] = {}     # user_id -> source (deep-link)

# Текст кнопок
BTN_ABOUT = "ℹ️ О бренде"
BTN_FEEDBACK = "📝 Оставить отзыв"
BTN_STAFF_VERIFY = "✅ Проверить/Погасить код"
BTN_ADMIN_ADD_STAFF = "➕ Добавить сотрудника"
BTN_CANCEL = "❌ Отмена"

def make_main_keyboard(user_id: int):
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # только «О бренде» и «Оставить отзыв»
    kb.add(telebot.types.KeyboardButton(BTN_ABOUT), telebot.types.KeyboardButton(BTN_FEEDBACK))
    # Кнопки для сотрудников/админа — дополнительно
    if not STAFF_IDS or (user_id in STAFF_IDS):
        kb.add(telebot.types.KeyboardButton(BTN_STAFF_VERIFY))
    if ADMIN_ID and user_id == ADMIN_ID:
        kb.add(telebot.types.KeyboardButton(BTN_ADMIN_ADD_STAFF))
    return kb

def inline_subscribe_keyboard():
    ikb = telebot.types.InlineKeyboardMarkup()
    ikb.add(telebot.types.InlineKeyboardButton("✅ Подписаться", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"))
    ikb.add(telebot.types.InlineKeyboardButton("🔄 Проверить подписку", callback_data="check_sub"))
    return ikb

# ---------- Вспомогательные функции ----------
def generate_short_code() -> str:
    # 4 символа из A-Z + 0-9, минимум 1 буква
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=4))
        if any(ch.isalpha() for ch in code):
            return code

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

def append_row_dict(ws, headers_list, data: dict):
    headers_now = ws.row_values(1)
    if not headers_now:
        ws.append_row(headers_list)
        headers_now = headers_list[:]
    row = [""] * len(headers_now)
    for k, v in data.items():
        if k in headers_now:
            row[headers_now.index(k)] = str(v)
    ws.append_row(row)

def ensure_subscribed_since(user_id):
    i, rec = get_row_by_user(user_id)
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    headers = sheet.row_values(1)
    if "SubscribedSince" not in headers:
        sheet.update_cell(1, len(headers) + 1, "SubscribedSince")
        headers = sheet.row_values(1)
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

def is_subscribed(user_id):
    try:
        m = bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

WELCOME = (
    "Привет! 👋 Это промо-бот <b>SBALO</b>.\n\n"
    "Подпишись на наш канал: {channel}\n"
    "После подписки, при желании, можешь проверить статус подписки — ссылка ниже.\n\n"
    "А ещё ты можешь узнать про бренд и оставить отзыв с помощью кнопок снизу."
)

BRAND_ABOUT = (
    "<b>SBALO</b> — это бренд, созданный, чтобы дарить высшую меру удовольствия от обуви и образа.\n"
    "Мы производим модную и комфортную обувь и аксессуары, вдохновляясь реальными городскими историями.\n\n"
    "• Качество и посадка — в приоритете\n"
    "• Стили — от casual до вечерних образов\n"
    "• Поддержка и уход — прямо из бота\n\n"
    f"Новости и релизы: {CHANNEL_USERNAME}"
)

# ---------- Общая логика проверки подписки ----------
def do_check_subscription(chat_id, user):
    if not is_subscribed(user.id):
        bot.send_message(
            chat_id,
            f"Подпишись на {CHANNEL_USERNAME}, затем нажми «Проверить подписку».",
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

# ---------- Handlers ----------
@bot.message_handler(commands=["start", "help"])
def start(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        USER_SOURCE[message.from_user.id] = parts[1].strip()[:32].lower()

    bot.send_message(
        message.chat.id,
        WELCOME.format(channel=CHANNEL_USERNAME),
        reply_markup=make_main_keyboard(message.from_user.id)
    )
    # inline-кнопки для подписки оставим в приветствии (но не в нижней клавиатуре)
    bot.send_message(
        message.chat.id,
        "Ссылка на канал и проверка подписки:",
        reply_markup=inline_subscribe_keyboard()
    )

@bot.callback_query_handler(func=lambda c: c.data == "check_sub")
def check_sub(cb):
    do_check_subscription(cb.message.chat.id, cb.from_user)
    try:
        bot.answer_callback_query(cb.id)
    except Exception:
        pass

# --- Новые кнопки: О бренде / Оставить отзыв ---
@bot.message_handler(func=lambda m: m.text == BTN_ABOUT)
def handle_about(message):
    bot.reply_to(message, BRAND_ABOUT, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == BTN_FEEDBACK)
def handle_feedback(message):
    STATE[message.from_user.id] = "await_feedback"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(
        message,
        "Напишите ваш отзыв одним сообщением и отправьте. Нажмите «Отмена», чтобы выйти.",
        reply_markup=kb
    )

# --- Кнопки персонала/админа (оставляем как было) ---
@bot.message_handler(func=lambda m: m.text == BTN_STAFF_VERIFY)
def handle_staff_verify(message):
    if STAFF_IDS and message.from_user.id not in STAFF_IDS:
        bot.reply_to(message, "Доступно только сотрудникам.")
        return
    STATE[message.from_user.id] = "await_code"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(message, "Введите промокод для проверки/погашения (4 символа) или нажмите «Отмена».", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == BTN_ADMIN_ADD_STAFF)
def handle_admin_add_staff(message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Доступно только администратору.")
        return
    STATE[message.from_user.id] = "await_staff_id"
    kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(telebot.types.KeyboardButton(BTN_CANCEL))
    bot.reply_to(
        message,
        "Пришлите ID пользователя-сотрудника (цифрами) или перешлите его любое сообщение. Либо «Отмена».",
        reply_markup=kb
    )

@bot.message_handler(func=lambda m: m.text == BTN_CANCEL)
def handle_cancel(message):
    STATE.pop(message.from_user.id, None)
    bot.reply_to(message, "Отменено.", reply_markup=make_main_keyboard(message.from_user.id))

@bot.message_handler(content_types=["text"])
def catch_all_text(message):
    uid = message.from_user.id
    state = STATE.get(uid)

    # Добавление сотрудника (админ)
    if state == "await_staff_id":
        new_id = None
        if hasattr(message, "forward_from") and message.forward_from:
            new_id = message.forward_from.id
        else:
            txt = (message.text or "").strip()
            if txt.isdigit():
                new_id = int(txt)

        if not new_id:
            bot.reply_to(message, "Не удалось определить ID. Пришлите число или перешлите сообщение от пользователя.")
            return

        STAFF_IDS.add(new_id)
        os.environ["STAFF_IDS"] = ",".join(str(x) for x in sorted(STAFF_IDS))
        STATE.pop(uid, None)
        bot.reply_to(message, f"Сотрудник добавлен: {new_id} ✅", reply_markup=make_main_keyboard(uid))
        return

    # Проверка/погашение кода (сотрудники)
    if state == "await_code":
        code = (message.text or "").strip().upper()
        if len(code) != 4 or not all(ch in (string.ascii_uppercase + string.digits) for ch in code):
            bot.reply_to(message, "Неверный формат. Введите 4 символа A–Z/0–9.")
            return
        ok, info = redeem_code(code, message.from_user.username or "Staff")
        STATE.pop(uid, None)
        bot.reply_to(message, info, parse_mode="HTML", reply_markup=make_main_keyboard(uid))
        return

    # Получение отзыва
    if state == "await_feedback":
        text = (message.text or "").strip()
        if not text:
            bot.reply_to(message, "Пустой отзыв не сохранён. Напишите текст или нажмите «Отмена».")
            return
        feedback_ws.append_row([
            str(uid),
            message.from_user.username or "",
            text,
            datetime.now().isoformat(sep=" ", timespec="seconds")
        ])
        STATE.pop(uid, None)
        bot.reply_to(
            message,
            "Спасибо за отзыв! Он сохранён ✅",
            reply_markup=make_main_keyboard(uid)
        )
        return

    # Нет активного состояния — просто подскажем про меню
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Используйте кнопки снизу 👇", reply_markup=make_main_keyboard(uid))
    else:
        bot.reply_to(message, "Выберите действие на клавиатуре ниже 👇", reply_markup=make_main_keyboard(uid))

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
    print("SBALO Promo Bot (Webhook) started on port", port)
    app.run(host="0.0.0.0", port=port)
