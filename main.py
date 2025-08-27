# -*- coding: utf-8 -*-
"""
SBALO Promo Bot — Webhook версия для Render
Изменения:
- Приветствие (WELCOME) и описание бренда (BRAND_ABOUT)
- Вернули проверку подписки и выдачу промокода по inline-кнопке
- Кнопка переименована: «🎁 Проверить подписку и получить промокод»
- Кнопку «✅ Проверить/Погасить код» видят только сотрудники из STAFF_IDS
- Отзывы: рейтинг + текст + фото (лист Feedback в Google Sheets)
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
STATE: Dict[int, str] = {}              # user_id -> state
USER_SOURCE: Dict[int, str] = {}        # user_id -> deep-link source
FEEDBACK_DRAFT: Dict[int, Dict] = {}    # черновики отзывов

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
    # Клиентские кнопки
    kb.add(telebot.types.KeyboardButton(BTN_ABOUT), telebot.types.KeyboardButton(BTN_FEEDBACK))
    # Кнопка сотрудника — только если user_id в STAFF_IDS
    if STAFF_IDS and user_id in STAFF_IDS:
        kb.add(telebot.types.KeyboardButton(BTN_STAFF_VERIFY))
    # Кнопка админа — только для ADMIN_ID
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

# Inline-клавиатура для проверки/выдачи промокода
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

# ---------- Утилиты (промо/подписка) ----------
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

def do_check_subscription(chat_id, user):
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

# ---------- Handlers ----------
@bot.message_handler(commands=["start", "help"])
def start(message):
    # deep-link source (если есть)
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        USER_SOURCE[message.from_user.id] = parts[1].strip()[:32].lower()

    # приветствие + нижняя клавиатура
    bot.send_message(
        message.chat.id,
        WELCOME,
        reply_markup=make_main_keyboard(message.from_user.id)
    )

    # дополнительное сообщение с inline-кнопкой для проверки/выдачи промокода
    bot.send_message(
        message.chat.id,
        "Хочешь промокод? Нажми кнопку ниже 👇",
        reply_markup=inline_subscribe_keyboard()
    )

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

    # текст отзыва
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

    # завершение отзыва (с фото или без)
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

    # добавление сотрудника (админ)
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

    # проверка/погашение кода (сотрудники)
    if state == "await_code":
        code = (message.text or "").strip().upper()
        if len(code) != 4 or not all(ch in (string.ascii_uppercase + string.digits) for ch in code):
            bot.reply_to(message, "Неверный формат. Введите 4 символа A–Z/0–9.")
            return
        ok, info = redeem_code(code, message.from_user.username or "Staff")
        STATE.pop(uid, None)
        bot.reply_to(message, info, parse_mode="HTML", reply_markup=make_main_keyboard(uid))
        return

    # нейтральная подсказка
    if message.text and message.text.startswith("/"):
        bot.reply_to(message, "Используйте кнопки снизу 👇", reply_markup=make_main_keyboard(uid))
    else:
        bot.reply_to(message, "Выберите действие на клавиатуре ниже 👇", reply_markup=make_main_keyboard(uid))

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

# Кнопки персонала/админа
@bot.message_handler(func=lambda m: m.text == BTN_STAFF_VERIFY)
def handle_staff_verify(message):
    # показывается только сотрудникам — но на всякий случай проверим ещё раз
    if not (STAFF_IDS and message.from_user.id in STAFF_IDS):
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
