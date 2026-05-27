import asyncio
import aiohttp
import logging
from datetime import datetime, timezone, timedelta
import os
import json
import re

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.error import TelegramError
from aiohttp import web

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.getenv("BOT_TOKEN",     "8693341837:AAF2lK6bGR3uoLz1kfkZt8IjDQIF18YXHN8")
CHANNEL_ID    = os.getenv("CHANNEL_ID",    "@yampilnews")
ALERT_API_KEY = os.getenv("ALERT_API_KEY", "b3de42c9:736017aa6745a605c155108e221d31a8")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "")
ADMIN_IDS     = set(int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip().isdigit())

TARGET_REGION_ID  = "31004"
ALERT_API_URL     = "https://api.alerts.in.ua/v1/alerts/active.json"
CHECK_INTERVAL    = 30
KYIV_TZ           = timezone(timedelta(hours=3))
PORT              = int(os.getenv("PORT", 10000))

MAP_URL           = "https://alerts.in.ua/"
MAP_IMAGE_URL     = "https://alerts.in.ua/map.png"
RENDER_URL        = os.getenv("RENDER_URL", "https://mapyampilalert.onrender.com")
WEBHOOK_PATH      = "/webhook"
WEBHOOK_URL       = f"{RENDER_URL}{WEBHOOK_PATH}"

REGISTERED_FILE   = "/tmp/registered_users.json"
BANNED_FILE       = "/tmp/banned_users.json"
QUESTIONS_FILE    = "/tmp/admin_questions.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

alert_active: bool | None = None
bot_stats = {"messages_sent": 0, "alerts_count": 0, "allclear_count": 0, "start_time": None}

AWAITING_NAME    = 1
AWAITING_PHONE   = 2
AWAITING_ADDRESS = 3
AWAITING_QUESTION = 4

# ─── USER MANAGEMENT ───────────────────────────────────────────────────────────
def load_users(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return set(json.load(f))
    return set()

def save_users(filename, users):
    with open(filename, "w") as f:
        json.dump(list(users), f)

def load_questions():
    if os.path.exists(QUESTIONS_FILE):
        with open(QUESTIONS_FILE, "r") as f:
            return json.load(f)
    return []

def save_questions(questions):
    with open(QUESTIONS_FILE, "w") as f:
        json.dump(questions, f)

registered_users = load_users(REGISTERED_FILE)
banned_users     = load_users(BANNED_FILE)

def is_registered(uid): return uid in registered_users
def is_banned(uid):     return uid in banned_users

def register_user(uid):
    registered_users.add(uid)
    save_users(REGISTERED_FILE, registered_users)

def ban_user(uid):
    banned_users.add(uid)
    save_users(BANNED_FILE, banned_users)

def unban_user(uid):
    banned_users.discard(uid)
    save_users(BANNED_FILE, banned_users)

def validate_phone(phone: str) -> bool:
    """Перевіряє чи номер телефону корректний"""
    phone = re.sub(r'\D', '', phone)
    return len(phone) >= 10

# ─── HELPERS ───────────────────────────────────────────────────────────────────
def now_kyiv():
    return datetime.now(KYIV_TZ)

def now_str():
    return now_kyiv().strftime("%H:%M %d.%m.%Y")

def uptime_str():
    if not bot_stats["start_time"]: return "невідомо"
    delta = now_kyiv() - bot_stats["start_time"]
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m = rem // 60
    return f"{h}г {m}хв"

def greeting():
    h = now_kyiv().hour
    if 5  <= h < 12: return "🌅 Доброго ранку"
    if 12 <= h < 17: return "☀️ Доброго дня"
    if 17 <= h < 22: return "🌅 Доброго вечора"
    return "🌙 Доброї ночі"

def user_name(update):
    u = update.effective_user
    return u.first_name if u and u.first_name else "друже"

def alert_status_text():
    if alert_active is None: return "⏳ перевіряємо..."
    return "🔴 АКТИВНА!" if alert_active else "✅ НЕМАЄ"

def is_admin(update):
    return update.effective_user.id in ADMIN_IDS

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 СТАТУС ТРИВОГИ",     callback_data="status"),
         InlineKeyboardButton("🗺️ ЖИВІ КАРТИ",          callback_data="map")],
        [InlineKeyboardButton("🏠 УКРИТТЯ",             callback_data="shelters"),
         InlineKeyboardButton("📞 ЕКСТРЕННІ НОМЕРИ",    callback_data="emergency")],
        [InlineKeyboardButton("📋 ПРАВИЛА ПОВЕДІНКИ",   callback_data="rules"),
         InlineKeyboardButton("📍 КОНТАКТИ ОТГ",        callback_data="contacts")],
        [InlineKeyboardButton("🔔 СПОВІЩЕННЯ",          callback_data="notifications"),
         InlineKeyboardButton("❓ ПИТАННЯ АДМІНУ",      callback_data="ask_question")],
        [InlineKeyboardButton("📊 ПРО БОТА",            callback_data="about")],
        [InlineKeyboardButton("📢 КАНАЛ НОВИН",         url="https://t.me/yampilnews")],
    ])

# ─── ALERT API ─────────────────────────────────────────────────────────────────
async def fetch_alert_status(session):
    headers = {"X-API-Key": ALERT_API_KEY}
    try:
        async with session.get(ALERT_API_URL, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            data = await resp.json()
        for a in data.get("alerts", []):
            if str(a.get("location_uid","")) == TARGET_REGION_ID and a.get("alert_type") == "air_raid":
                return True
        return False
    except Exception as e:
        log.error("API error: %s", e)
        return alert_active or False

async def post_to_channel(bot, text):
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=text)
        bot_stats["messages_sent"] += 1
    except TelegramError as e:
        log.error("Telegram error: %s", e)

# ─── ACCESS CONTROL ───────────────────────────────────────────────────────────
async def check_access(update):
    uid = update.effective_user.id
    if is_banned(uid):
        await update.message.reply_text("🚫 Вам заборонено використовувати цього бота.")
        return False
    if not is_registered(uid) and not is_admin(update):
        await update.message.reply_text(
            "👋 Привіт! Ти новий користувач.\n\n"
            "🔐 Спочатку потрібна реєстрація.\n\n"
            "Натисни /register щоб розпочати."
        )
        return False
    return True

# ─── REGISTRATION ──────────────────────────────────────────────────────────────
async def cmd_register(update, ctx):
    uid = update.effective_user.id
    if is_registered(uid):
        await update.message.reply_text("✅ Ти вже зареєстрований!")
        return ConversationHandler.END
    if is_banned(uid):
        await update.message.reply_text("🚫 Реєстрація недоступна.")
        return ConversationHandler.END
    await update.message.reply_text(
        "📝 РЕЄСТРАЦІЯ НА БОТ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Як тебе звати?"
    )
    return AWAITING_NAME

async def receive_name(update, ctx):
    ctx.user_data["name"] = update.message.text
    await update.message.reply_text("2️⃣ Твій номер телефону?\n\n(Приклад: +380 95 123 45 67)")
    return AWAITING_PHONE

async def receive_phone(update, ctx):
    phone = update.message.text
    if not validate_phone(phone):
        await update.message.reply_text("❌ Невірний номер. Спробуй ще раз.")
        return AWAITING_PHONE
    ctx.user_data["phone"] = phone
    await update.message.reply_text("3️⃣ Твоя адреса (селище/вулиця)?")
    return AWAITING_ADDRESS

async def receive_address(update, ctx):
    ctx.user_data["address"] = update.message.text
    uid = update.effective_user.id
    register_user(uid)
    await update.message.reply_text(
        "✅ РЕЄСТРАЦІЯ УСПІШНА!\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ти тепер маєш доступ до всіх функцій.\n\n"
        "Натисни /start щоб розпочати."
    )
    return ConversationHandler.END

async def cancel_registration(update, ctx):
    await update.message.reply_text("❌ Реєстрація скасована.")
    return ConversationHandler.END

# ─── КОМАНДИ ───────────────────────────────────────────────────────────────────
async def cmd_start(update, ctx):
    if not await check_access(update): return
    name   = user_name(update)
    gr     = greeting()
    status = alert_status_text()
    
    text = (
        f"{gr}, {name}! 👋\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 СМТ ЯМПІЛЬ\n"
        f"Шепетівський р-н, Хмельницька обл.\n\n"
        f"🚨 СТАТУС ТРИВОГИ: {status}\n"
    )
    if alert_active:
        text += f"\n⚠️ ПРОШУ ПРОЙТИ ДО УКРИТТЯ!\n🙏 БЕРЕЖІТЬ СЕБЕ!\n"
    text += f"\n━━━━━━━━━━━━━━━━━━━━━━━━\n\nОберіть дію з меню:"
    
    await update.message.reply_text(text, reply_markup=main_keyboard())
    bot_stats["messages_sent"] += 1

async def cmd_status(update, ctx):
    if not await check_access(update): return
    status = alert_status_text()
    
    text = (
        "🛡️ СТАТУС ПОВІТРЯНОЇ ТРИВОГИ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📍 Шепетівський р-н (СМТ Ямпіль)\n"
        f"🕐 {now_str()}\n\n"
        f"⚡ СТАТУС: {status}\n"
    )
    if alert_active:
        text += f"\n⚠️ ТРИВОГА АКТИВНА!\n"
        text += f"🏃 Рухайтесь до укриття!\n"
        text += f"🙏 БЕРЕЖІТЬ СЕБЕ!\n"
    
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 ОНОВИТИ", callback_data="status"),
        InlineKeyboardButton("🗺️ КАРТА", callback_data="map"),
    ]])
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, reply_markup=kb)

async def cmd_map(update, ctx):
    if not await check_access(update): return
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    status = alert_status_text()
    
    caption = (
        f"🗺️ ЖИВІ КАРТИ ТРИВОГ УКРАЇНИ\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🕐 Час оновлення: {now_str()}\n"
        f"📍 СМТ Ямпіль: {status}\n\n"
        f"🔴 активна тривога\n"
        f"🟢 спокійно\n"
    )
    
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌐 ОНЛАЙН КАРТА", url=MAP_URL),
        InlineKeyboardButton("🔄 ОНОВИТИ", callback_data="map"),
    ]])
    
    try:
        await msg.reply_photo(photo=MAP_IMAGE_URL, caption=caption, reply_markup=kb)
    except Exception:
        await msg.reply_text(caption, reply_markup=kb)

async def cmd_shelters(update, ctx):
    if not await check_access(update): return
    text = (
        "🏠 УКРИТТЯ В СМТ ЯМПІЛЬ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Підвал Гімназії\n"
        "   📍 вул. Шкільна, 1\n\n"
        "2️⃣ Будинок Культури\n"
        "   📍 вул. Центральна\n\n"
        "3️⃣ Амбулаторія\n"
        "   📍 вул. Медична\n\n"
        "4️⃣ ОТГ Адмін\n"
        "   📍 вул. Незалежності\n\n"
        "⚠️ ПРИ ТРИВОЗІ РУХАЙТЕСЬ \n"
        "ДО НАЙБЛИЖЧОГО УКРИТТЯ!"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

async def cmd_emergency(update, ctx):
    if not await check_access(update): return
    text = (
        "📞 ЕКСТРЕНІ ТЕЛЕФОНИ\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "🚒 Пожежна: 101\n"
        "🚔 Поліція: 102\n"
        "🚑 Швидка: 103\n"
        "🛡️ ДСНС: 104\n"
        "☎️ Єдиний: 112\n\n"
        "🇺🇦 МО Гаряча лінія:\n"
        "   1580\n\n"
        "📻 Слідкуйте за офіційними джерелами!"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

async def cmd_rules(update, ctx):
    if not await check_access(update): return
    text = (
        "📜 ПРАВИЛА ПОВЕДІНКИ\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ Припиніть ВСЕ негайно\n"
        "2️⃣ Перейдіть до укриття\n"
        "3️⃣ Ляжте біля стіни\n"
        "4️⃣ Подалі від вікон\n"
        "5️⃣ Вимкніть газ\n"
        "6️⃣ Візьміть документи\n"
        "7️⃣ Не виходьте рано\n"
        "8️⃣ Допоможіть іншим\n\n"
        "🏃 ПІСЛЯ ВИБУХУ:\n"
        "• Ляжте на підлогу\n"
        "• Прикрийте голову\n"
        "• Чекайте рятувальників\n\n"
        "🙏 БЕРЕЖІТЬ СЕБЕ!"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

async def cmd_contacts(update, ctx):
    if not await check_access(update): return
    text = (
        "📍 КОНТАКТИ ОТГ\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🏛️ Ямпільська селищна громада\n"
        "   🌐 https://yampil-gmada.gov.ua/\n\n"
        "📍 м. Ямпіль, вул. Незалежності\n\n"
        "🚨 Місцева поліція: 102\n"
        "🏥 Медицина: 103\n\n"
        "ℹ️ Уточнюйте контакти в місцевій адміністрації"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

async def cmd_notifications(update, ctx):
    if not await check_access(update): return
    text = (
        "🔔 СПОВІЩЕННЯ\n"
        "━━━━━━━━━━━━━━\n\n"
        "✅ Ти отримуватимеш:\n"
        "• Оголошення тривоги\n"
        "• Відбій тривоги\n"
        "• Оголошення ОТГ\n\n"
        "💡 ПОРАДИ:\n"
        "• Активуй звук\n"
        "• Не вимикай уведомлення\n"
        "• Ділись інформацією\n\n"
        "📢 @yampilnews"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 КАНАЛ", url="https://t.me/yampilnews")]])
    msg = update.message or update.callback_query.message
    await msg.reply_text(text, reply_markup=kb)

async def cmd_ask_question(update, ctx):
    if not await check_access(update): return
    await update.callback_query.message.reply_text(
        "❓ ПИТАННЯ АДМІНІСТРАЦІЇ\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Напиши своє питання, і адміністрація відповість тобі найскоріше.\n\n"
        "/ask_question <текст>"
    )

async def cmd_ask(update, ctx):
    if not await check_access(update): return
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text(
            "❓ Використання: /ask_question <твоє питання>\n\n"
            "Приклад:\n/ask_question Де найближче укриття?"
        )
        return
    
    uid = update.effective_user.id
    name = user_name(update)
    
    questions = load_questions()
    questions.append({
        "user_id": uid,
        "name": name,
        "text": text,
        "time": now_str()
    })
    save_questions(questions)
    
    await update.message.reply_text(
        "✅ ПИТАННЯ ОТРИМАНО!\n"
        "━━━━━━━━━━━━━━━━━━━\n\n"
        "Адміністрація скоро відповість на твоє питання."
    )
    
    # Надсилаємо адміну
    for admin_id in ADMIN_IDS:
        try:
            await ctx.bot.send_message(
                chat_id=admin_id,
                text=f"❓ НОВЕ ПИТАННЯ\n\n👤 {name} ({uid})\n📝 {text}\n\n🕐 {now_str()}"
            )
        except:
            pass

async def cmd_about(update, ctx):
    if not await check_access(update): return
    text = (
        "ℹ️ ПРО БОТА\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🤖 Бот моніторингу тривог\n"
        "📍 СМТ Ямпіль\n"
        "   Шепетівський р-н\n"
        "   Хмельницька область\n\n"
        "⚡ МОЖЛИВОСТІ:\n"
        "✅ Моніторинг 24/7\n"
        "✅ Автопубліка в канал\n"
        "✅ Живі карти тривог\n"
        "✅ Реєстрація користувачів\n"
        "✅ Питання адміністрації\n\n"
        f"⏱ Аптайм: {uptime_str()}\n"
        f"🚨 Тривог: {bot_stats['alerts_count']}\n"
        f"📨 Повідомлень: {bot_stats['messages_sent']}\n\n"
        "📡 Дані: alerts.in.ua\n"
        "📢 Канал: @yampilnews\n\n"
        "Слава Україні! 🇺🇦"
    )
    msg = update.message or update.callback_query.message
    await msg.reply_text(text)

async def cmd_help(update, ctx):
    if not await check_access(update): return
    text = (
        "📋 ВСІ КОМАНДИ\n"
        "━━━━━━━━━━━━━━━\n\n"
        "/start — меню\n"
        "/status — статус\n"
        "/map — карти\n"
        "/shelters — укриття\n"
        "/emergency — номери\n"
        "/rules — правила\n"
        "/contacts — контакти\n"
        "/ask_question — питання\n"
        "/about — про бота\n"
        "/help — цей список\n"
        "/register — реєстрація\n"
        "/myid — твій ID\n"
    )
    await update.message.reply_text(text)

async def cmd_myid(update, ctx):
    uid = update.effective_user.id
    name = user_name(update)
    role = "🔐 Адміністратор" if is_admin(update) else ("✅ Зареєстрований" if is_registered(uid) else "❌ Не зареєстрований")
    await update.message.reply_text(
        f"👤 {name}\n🆔 ID: `{uid}`\n\n{role}",
        parse_mode="Markdown"
    )

async def cmd_admin(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    
    name = user_name(update)
    questions = load_questions()
    
    text = (
        f"🔐 ПАНЕЛЬ АДМІНІСТРАТОРА\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👤 {name}\n🕐 {now_str()}\n\n"
        f"📊 СТАТУС:\n"
        f"{'🔴 ТРИВОГА!' if alert_active else '🟢 СПОКІЙНО'}\n"
        f"⏱ Аптайм: {uptime_str()}\n\n"
        f"📈 СТАТИСТИКА:\n"
        f"🚨 Тривог: {bot_stats['alerts_count']}\n"
        f"✅ Відбоїв: {bot_stats['allclear_count']}\n"
        f"📨 Повідомлень: {bot_stats['messages_sent']}\n"
        f"👥 Користувачів: {len(registered_users)}\n"
        f"🚫 Заблокованих: {len(banned_users)}\n"
        f"❓ Нових питань: {len(questions)}"
    )
    
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Оголошення",        callback_data="admin_post")],
        [InlineKeyboardButton("🚨 Тест тривога",      callback_data="admin_test_alert"),
         InlineKeyboardButton("✅ Тест відбій",       callback_data="admin_test_clear")],
        [InlineKeyboardButton("👥 Користувачі",       callback_data="admin_list"),
         InlineKeyboardButton("❓ Питання",           callback_data="admin_questions")],
        [InlineKeyboardButton("🚫 /ban <id>",         callback_data="admin_ban"),
         InlineKeyboardButton("🔓 /unban <id>",       callback_data="admin_unban")],
    ])
    await update.message.reply_text(text, reply_markup=kb)

async def cmd_post(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text("✍️ /post <текст>")
        return
    await post_to_channel(ctx.bot, f"📢 ОГОЛОШЕННЯ\n━━━━━━━━━━\n\n{text}\n\n🕐 {now_str()}")
    await update.message.reply_text("✅ Опубліковано!")

async def cmd_ban(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("/ban <user_id>")
        return
    uid = int(ctx.args[0])
    ban_user(uid)
    await update.message.reply_text(f"🚫 Користувач {uid} заблокований.")

async def cmd_unban(update, ctx):
    if not is_admin(update):
        await update.message.reply_text("🚫 Немає прав.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text("/unban <user_id>")
        return
    uid = int(ctx.args[0])
    unban_user(uid)
    await update.message.reply_text(f"✅ Користувач {uid} розблокований.")

# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────
async def button_handler(update, ctx):
    query = update.callback_query
    await query.answer()
    data = query.data

    if   data == "status":        await cmd_status(update, ctx)
    elif data == "map":           await cmd_map(update, ctx)
    elif data == "shelters":      await cmd_shelters(update, ctx)
    elif data == "emergency":     await cmd_emergency(update, ctx)
    elif data == "rules":         await cmd_rules(update, ctx)
    elif data == "contacts":      await cmd_contacts(update, ctx)
    elif data == "notifications": await cmd_notifications(update, ctx)
    elif data == "ask_question":  await cmd_ask_question(update, ctx)
    elif data == "about":         await cmd_about(update, ctx)
    
    elif data == "admin_post":
        if not is_admin(update): return
        await query.message.reply_text("✍️ /post <текст>")
    
    elif data == "admin_test_alert":
        if not is_admin(update): return
        await post_to_channel(ctx.bot,
            f"‼️ УВАГА! ТРИВОГА!\n━━━━━━━━━━━━━━━\n\n"
            f"Станом на {now_str()}, в ОТГ селища Ямпіль\n"
            f"ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА!\n\n"
            f"⚠️ ПРОШУ ПРОЙТИ ДО УКРИТТЯ!\n"
            f"🙏 БЕРЕЖІТЬ СЕБЕ!\n\n"
            f"🔧 [ТЕСТОВЕ ПОВІДОМЛЕННЯ]")
        await query.message.reply_text("✅ Надіслано!")
    
    elif data == "admin_test_clear":
        if not is_admin(update): return
        await post_to_channel(ctx.bot,
            f"✅ ВІДБІЙ!\n━━━━━━━━━━\n\n"
            f"Станом на {now_str()} був оголошений\n"
            f"ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ.\n\n"
            f"🟢 СПОКІЙНО\n\n"
            f"🔧 [ТЕСТОВЕ ПОВІДОМЛЕННЯ]")
        await query.message.reply_text("✅ Надіслано!")
    
    elif data == "admin_list":
        if not is_admin(update): return
        await query.message.reply_text(
            f"👥 КОРИСТУВАЧІ\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"✅ Зареєстровано: {len(registered_users)}\n"
            f"🚫 Заблокованих: {len(banned_users)}")
    
    elif data == "admin_questions":
        if not is_admin(update): return
        questions = load_questions()
        if not questions:
            await query.message.reply_text("❓ Питань немає")
            return
        text = "❓ ПИТАННЯ:\n━━━━━━━━━━━\n\n"
        for q in questions[-5:]:
            text += f"👤 {q['name']}\n📝 {q['text']}\n🕐 {q['time']}\n\n"
        await query.message.reply_text(text)
    
    elif data == "admin_ban":
        if not is_admin(update): return
        await query.message.reply_text("🚫 /ban <user_id>")
    
    elif data == "admin_unban":
        if not is_admin(update): return
        await query.message.reply_text("🔓 /unban <user_id>")

# ─── ALERT LOOP ────────────────────────────────────────────────────────────────
async def alert_check_loop(bot, session):
    global alert_active
    log.info("Alert loop запущено")
    while True:
        try:
            current = await fetch_alert_status(session)
            if alert_active is None:
                alert_active = current
                log.info("Початковий стан: %s", current)
            elif current and not alert_active:
                alert_active = True
                bot_stats["alerts_count"] += 1
                await post_to_channel(bot,
                    f"‼️ УВАГА! ТРИВОГА!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"Станом на {now_str()}, в ОТГ селища Ямпіль\n"
                    f"ОГОЛОШЕНА ПОВІТРЯНА ТРИВОГА!\n\n"
                    f"⚠️ ПРОШУ ПРОЙТИ ДО УКРИТТЯ!\n"
                    f"🙏 БЕРЕЖІТЬ СЕБЕ!")
                try:
                    await bot.send_photo(chat_id=CHANNEL_ID, photo=MAP_IMAGE_URL,
                                        caption=f"🗺️ ЖИВІ КАРТИ ТРИВОГ\n━━━━━━━━━━━━━━\n\n🕐 {now_str()}\n\n{MAP_URL}")
                except Exception as e:
                    log.warning("Помилка із картою: %s", e)
            elif not current and alert_active:
                alert_active = False
                bot_stats["allclear_count"] += 1
                await post_to_channel(bot,
                    f"✅ ВІДБІЙ!\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"Станом на {now_str()} був оголошений\n"
                    f"ВІДБІЙ ПОВІТРЯНОЇ ТРИВОГИ.\n\n"
                    f"🟢 СПОКІЙНО")
        except Exception as e:
            log.error("Alert loop помилка: %s", e)
        await asyncio.sleep(CHECK_INTERVAL)

# ─── WEBHOOK + HTTP ────────────────────────────────────────────────────────────
tg_app = None

async def handle_webhook(request):
    try:
        data = await request.json()
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)
        return web.Response(status=200)
    except Exception as e:
        log.error("Webhook помилка: %s", e)
        return web.Response(status=500)

async def health_check(request):
    return web.json_response({"status": "ok", "alert_active": alert_active, "time": now_str()})

# ─── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    global tg_app
    bot_stats["start_time"] = now_kyiv()

    tg_app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("register", cmd_register)],
        states={
            AWAITING_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
            AWAITING_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_phone)],
            AWAITING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_address)],
        },
        fallbacks=[CommandHandler("cancel", cancel_registration)],
    )

    tg_app.add_handler(conv_handler)
    tg_app.add_handler(CommandHandler("start",           cmd_start))
    tg_app.add_handler(CommandHandler("status",          cmd_status))
    tg_app.add_handler(CommandHandler("map",             cmd_map))
    tg_app.add_handler(CommandHandler("shelters",        cmd_shelters))
    tg_app.add_handler(CommandHandler("emergency",       cmd_emergency))
    tg_app.add_handler(CommandHandler("rules",           cmd_rules))
    tg_app.add_handler(CommandHandler("contacts",        cmd_contacts))
    tg_app.add_handler(CommandHandler("notifications",   cmd_notifications))
    tg_app.add_handler(CommandHandler("ask_question",    cmd_ask))
    tg_app.add_handler(CommandHandler("about",           cmd_about))
    tg_app.add_handler(CommandHandler("help",            cmd_help))
    tg_app.add_handler(CommandHandler("myid",            cmd_myid))
    tg_app.add_handler(CommandHandler("admin",           cmd_admin))
    tg_app.add_handler(CommandHandler("post",            cmd_post))
    tg_app.add_handler(CommandHandler("ban",             cmd_ban))
    tg_app.add_handler(CommandHandler("unban",           cmd_unban))
    tg_app.add_handler(CallbackQueryHandler(button_handler))

    await tg_app.initialize()

    async with aiohttp.ClientSession() as session:
        tg_app.bot_data["session"] = session

        await tg_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        log.info(f"Webhook: {WEBHOOK_URL}")

        asyncio.create_task(alert_check_loop(tg_app.bot, session))

        http_app = web.Application()
        http_app.router.add_post(WEBHOOK_PATH, handle_webhook)
        http_app.router.add_get("/health", health_check)
        http_app.router.add_get("/", health_check)

        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()

        log.info(f"🚀 Сервер на порту {PORT}")
        log.info(f"👥 Адмінів: {len(ADMIN_IDS)}, Користувачів: {len(registered_users)}")

        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
